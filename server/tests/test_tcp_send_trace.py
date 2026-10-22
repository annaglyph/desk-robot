"""Speech-transfer trace: Darwin TCP_CONNECTION_INFO and buffered timestamps."""

import asyncio
import json
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import main
from brain.tcp_probe import (
    DARWIN_TCP_CONNECTION_INFO_SIZE,
    SpeechSendTrace,
    parse_darwin_tcp_connection_info,
    read_send_path,
)


def _darwin_buffer(**fields: int) -> bytes:
    buf = bytearray(DARWIN_TCP_CONNECTION_INFO_SIZE)
    buf[24:28] = fields["snd_cwnd"].to_bytes(4, "little")
    buf[28:32] = fields["snd_wnd"].to_bytes(4, "little")
    buf[32:36] = fields["snd_sbbytes"].to_bytes(4, "little")
    buf[64:72] = fields["txbytes"].to_bytes(8, "little")
    buf[72:80] = fields["txretransmitbytes"].to_bytes(8, "little")
    return bytes(buf)


class DarwinTcpInfoTests(unittest.TestCase):
    def test_known_offsets_are_byte_fields(self):
        parsed = parse_darwin_tcp_connection_info(_darwin_buffer(
            snd_cwnd=163320,
            snd_wnd=5760,
            snd_sbbytes=3205,
            txbytes=534892,
            txretransmitbytes=1436,
        ))
        self.assertEqual(parsed, {
            "snd_cwnd": 163320,
            "snd_wnd": 5760,
            "snd_sbbytes": 3205,
            "txbytes": 534892,
            "txretransmitbytes": 1436,
        })

    def test_short_buffer_leaves_missing_fields_empty(self):
        parsed = parse_darwin_tcp_connection_info(_darwin_buffer(
            snd_cwnd=10, snd_wnd=20, snd_sbbytes=30, txbytes=40, txretransmitbytes=50,
        )[:36])
        self.assertEqual(parsed["snd_cwnd"], 10)
        self.assertEqual(parsed["snd_wnd"], 20)
        self.assertEqual(parsed["snd_sbbytes"], 30)
        self.assertIsNone(parsed["txbytes"])
        self.assertIsNone(parsed["txretransmitbytes"])

    def test_missing_socket_is_reported(self):
        state = read_send_path(None)
        self.assertEqual(state["tcp_error"], "no_socket")
        self.assertIsNone(state["snd_wnd"])
        self.assertIsNone(state["txretransmitbytes"])

    def test_unsupported_socket_is_reported(self):
        class Rejects:
            def getsockopt(self, *args):
                raise OSError(102, "Operation not supported on socket")

        state = read_send_path(Rejects())
        self.assertEqual(state["tcp_error"], "Operation not supported on socket")
        self.assertIsNone(state["snd_cwnd"])
        self.assertIsNone(state["txretransmitbytes"])

    def test_live_tcp_socket_uses_byte_counters(self):
        if not hasattr(socket, "TCP_CONNECTION_INFO"):
            self.skipTest("TCP_CONNECTION_INFO is not available")
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def accept():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                conn.recv(16)
            except OSError:
                pass
            finally:
                conn.close()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        client = socket.socket()
        try:
            client.connect(("127.0.0.1", port))
            client.send(b"x")
            state = read_send_path(client)
        finally:
            client.close()
            server.close()
            thread.join(timeout=1)
        self.assertIsNone(state["tcp_error"])
        self.assertEqual(state["tcp_bytes"], DARWIN_TCP_CONNECTION_INFO_SIZE)
        self.assertGreater(state["snd_wnd"], 0)
        self.assertGreater(state["snd_cwnd"], 0)
        self.assertGreaterEqual(state["txbytes"], 1)
        self.assertEqual(state["txretransmitbytes"], 0)
        self.assertGreaterEqual(state["snd_sbbytes"], 0)


class SpeechSendTraceTests(unittest.TestCase):
    def test_records_are_buffered_until_finish(self):
        directory = Path(self._dir())
        trace = SpeechSendTrace(lambda: 4, lambda: {
            "snd_wnd": 5760, "snd_cwnd": 5760, "snd_sbbytes": 0,
            "txbytes": 0, "txretransmitbytes": 0, "tcp_error": None, "tcp_bytes": 112,
        }, sample_s=0.05)
        with patch.dict(os.environ, {"DESK_ROBOT_TCP_TRACE_DIR": str(directory)}):
            before = list(directory.glob("speech-*.jsonl"))
            trace.start()
            trace.add_frame(3205, 1.0, 100.0, 1.001, 100.001, 1.05, 100.05, 0, 12)
            time.sleep(0.12)
            self.assertEqual(list(directory.glob("speech-*.jsonl")), before)
            path = trace.finish()
        self.assertIsNotNone(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        self.assertEqual(header["kind"], "header")
        self.assertEqual(header["units"]["snd_cwnd"], "bytes")
        self.assertEqual(header["units"]["txretransmitbytes"], "bytes")
        self.assertIn("Not Linux TCP_INFO", header["source"])
        kinds = [json.loads(line)["kind"] for line in lines[1:]]
        self.assertIn("frame", kinds)
        self.assertIn("sample", kinds)
        frame = next(json.loads(line) for line in lines[1:] if json.loads(line)["kind"] == "frame")
        self.assertLessEqual(frame["ready_mono"], frame["send_mono"])
        self.assertLessEqual(frame["send_mono"], frame["done_mono"])
        self.assertEqual(frame["ready_wall"], 100.0)
        self.assertEqual(frame["nbytes"], 3205)
        sample = next(json.loads(line) for line in lines[1:] if json.loads(line)["kind"] == "sample")
        self.assertEqual(sample["buf"], 4)
        self.assertEqual(sample["snd_wnd"], 5760)
        self.assertEqual(sample["snd_cwnd"], 5760)
        self.assertEqual(sample["snd_sbbytes"], 0)
        self.assertEqual(sample["txretransmitbytes"], 0)
        self.assertIn("wall", sample)
        self.assertIn("mono", sample)

    def _dir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


class PlayRobotTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_writes_the_trace_after_the_sends(self):
        directory = Path(self._dir())
        sent = []

        class FakeSocket:
            def __init__(self):
                self.transport = self

            async def send(self, frame):
                sent.append(frame)
                await asyncio.sleep(0.01)

            def get_write_buffer_size(self):
                return len(sent)

            def get_extra_info(self, name):
                return None

        async def send_to_robot(payload):
            if payload.get("type") == "speak_end":
                main.robot_speak_done.set()
            return True

        previous = main.robot_socket
        main.robot_socket = FakeSocket()
        reply = main.SpokenReply(asyncio.get_running_loop(), main.Timeline(time.time()))

        async def no_more():
            return None

        reply._next = no_more
        try:
            with patch.dict(os.environ, {"DESK_ROBOT_TCP_TRACE_DIR": str(directory)}), \
                    patch.object(main, "send_to_robot", send_to_robot):
                await reply._play_robot(b"\x00\x01" * 8)
        finally:
            main.robot_socket = previous
            main.robot_speak_done.clear()
        self.assertEqual(sent, [b"\x01" + b"\x00\x01" * 8])
        files = list(directory.glob("speech-*.jsonl"))
        self.assertEqual(len(files), 1)
        records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        frame = next(rec for rec in records if rec["kind"] == "frame")
        self.assertLessEqual(frame["ready_wall"], frame["send_wall"])
        self.assertLessEqual(frame["send_wall"], frame["done_wall"])
        self.assertEqual(frame["nbytes"], 17)
        sample = next(rec for rec in records if rec["kind"] == "sample")
        self.assertEqual(sample["tcp_error"], "no_socket")
        self.assertIsNone(sample["snd_wnd"])

    def _dir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path
