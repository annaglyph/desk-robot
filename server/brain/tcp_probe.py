"""Optional speech-transfer trace for the robot WebSocket.

Off unless ``DESK_ROBOT_TCP_TRACE=1``. When on, it samples the asyncio write
buffer and the macOS TCP send path while a reply is being sent, then writes
one JSONL file after the utterance. It does not change socket options, frame
size, or write-buffer limits. ``DESK_ROBOT_TCP_TRACE_DIR`` overrides the
output directory.

The TCP fields come from Darwin's ``struct tcp_connection_info``
(``netinet/tcp.h``, ``TCP_CONNECTION_INFO`` / 0x106). Offsets and units were
checked against the active macOS SDK: ``sizeof`` is 112, and a live socket
reports the congestion window and send-buffer occupancy in bytes. Linux
``TCP_INFO`` is a different struct (congestion window in segments, retransmits
as a count) and is not read.

    offset  size  field                    unit
        24     4  tcpi_snd_cwnd            bytes
        28     4  tcpi_snd_wnd             bytes
        32     4  tcpi_snd_sbbytes         bytes, including in-flight data
        64     8  tcpi_txbytes             bytes
        72     8  tcpi_txretransmitbytes   bytes
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Little-endian layout of the Darwin struct. Apple platforms are little-endian.
_OFF_SND_CWND = 24
_OFF_SND_WND = 28
_OFF_SND_SBBYTES = 32
_OFF_TXBYTES = 64
_OFF_TXRETRANSMITBYTES = 72
DARWIN_TCP_CONNECTION_INFO_SIZE = 112

_TCP_FIELDS = ("snd_wnd", "snd_cwnd", "snd_sbbytes", "txbytes", "txretransmitbytes")


def parse_darwin_tcp_connection_info(buf: bytes) -> dict:
    """Read the send-path fields. A short buffer leaves the missing ones as None."""
    return {
        "snd_cwnd": _u32(buf, _OFF_SND_CWND),
        "snd_wnd": _u32(buf, _OFF_SND_WND),
        "snd_sbbytes": _u32(buf, _OFF_SND_SBBYTES),
        "txbytes": _u64(buf, _OFF_TXBYTES),
        "txretransmitbytes": _u64(buf, _OFF_TXRETRANSMITBYTES),
    }


def read_send_path(sock) -> dict:
    """Snapshot of the kernel send path. Missing support yields null fields.

    ``tcp_error`` is set when the socket cannot be queried. A buffer whose
    length is not the size checked against the SDK is still parsed at the
    known offsets, and ``tcp_bytes`` records what the kernel returned.
    """
    state = {name: None for name in _TCP_FIELDS}
    state["tcp_error"] = None
    state["tcp_bytes"] = None
    if sock is None:
        state["tcp_error"] = "no_socket"
        return state
    option = getattr(socket, "TCP_CONNECTION_INFO", None)
    if option is None:
        state["tcp_error"] = "unsupported"
        return state
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, option, 256)
    except OSError as exc:
        state["tcp_error"] = exc.strerror or str(exc)
        return state
    if not isinstance(raw, (bytes, bytearray)):
        state["tcp_error"] = "unsupported"
        return state
    state.update(parse_darwin_tcp_connection_info(bytes(raw)))
    state["tcp_bytes"] = len(raw)
    return state


def tcp_trace_enabled() -> bool:
    """True only when DESK_ROBOT_TCP_TRACE is set to 1, true, yes, or on."""
    return os.environ.get("DESK_ROBOT_TCP_TRACE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def default_trace_dir() -> Path:
    override = os.environ.get("DESK_ROBOT_TCP_TRACE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "experiments" / "out" / "tcp-send"


class SpeechSendTrace:
    """Buffer of frame timestamps and ~50 ms socket samples for one utterance.

    The sampler runs on its own thread so a stall on the asyncio loop does not
    hide the kernel's view, and so the loop never does the file write. Frame
    marks are appended from the loop; both sides only take a short lock.
    """

    def __init__(self, buffer_size, tcp_state, sample_s: float = 0.05) -> None:
        self._buffer_size = buffer_size
        self._tcp_state = tcp_state
        self.sample_s = sample_s
        self._records: list[dict] = []
        self._frames = 0
        self._samples = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_wall = 0.0
        self.started_mono = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="tcp-send-trace", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def add_frame(
        self,
        nbytes: int,
        ready_mono: float,
        ready_wall: float,
        send_mono: float,
        send_wall: float,
        done_mono: float,
        done_wall: float,
        buf_send: int | None,
        buf_done: int | None,
    ) -> None:
        with self._lock:
            record = {
                "kind": "frame",
                "i": self._frames,
                "nbytes": nbytes,
                "ready_mono": ready_mono,
                "ready_wall": ready_wall,
                "send_mono": send_mono,
                "send_wall": send_wall,
                "done_mono": done_mono,
                "done_wall": done_wall,
                "buf_send": buf_send,
                "buf_done": buf_done,
            }
            self._frames += 1
            self._records.append(record)

    def finish(self) -> Path | None:
        """Stop sampling and write the buffered records. No samples means no file."""
        self.stop()
        with self._lock:
            if not self._records:
                return None
            records = list(self._records)
            frames = self._frames
            samples = self._samples
            started_wall = self.started_wall
            started_mono = self.started_mono
        directory = default_trace_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(started_wall).strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"speech-{stamp}.jsonl"
        header = {
            "kind": "header",
            "wall": started_wall,
            "mono": started_mono,
            "platform": sys.platform,
            "sample_s": self.sample_s,
            "frames": frames,
            "samples": samples,
            "tcp_option": "TCP_CONNECTION_INFO",
            "tcp_sizeof": DARWIN_TCP_CONNECTION_INFO_SIZE,
            "units": {
                "buf": "bytes",
                "buf_send": "bytes",
                "buf_done": "bytes",
                "snd_wnd": "bytes",
                "snd_cwnd": "bytes",
                "snd_sbbytes": "bytes",
                "txbytes": "bytes",
                "txretransmitbytes": "bytes",
            },
            "clock": "wall is time.time() Unix seconds; mono is time.monotonic() seconds",
            "source": (
                "Darwin netinet/tcp.h tcp_connection_info. "
                "snd_wnd, snd_cwnd and snd_sbbytes are bytes. "
                "Not Linux TCP_INFO."
            ),
        }
        ordered = sorted(records, key=lambda rec: rec.get("ready_mono", rec.get("mono", 0.0)))
        with path.open("w", encoding="utf-8") as out:
            out.write(_dumps(header) + "\n")
            for record in ordered:
                out.write(_dumps(record) + "\n")
        return path

    def _run(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            delay = next_at - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            try:
                self._take_sample()
            except Exception as exc:
                self._append({
                    "kind": "sample",
                    "mono": time.monotonic(),
                    "wall": time.time(),
                    "buf": None,
                    "tcp_error": str(exc),
                })
            next_at += self.sample_s
            now = time.monotonic()
            if next_at < now:
                next_at = now

    def _take_sample(self) -> None:
        mono = time.monotonic()
        wall = time.time()
        try:
            buf = self._buffer_size()
        except Exception:
            buf = None
        try:
            tcp = self._tcp_state()
        except Exception as exc:
            tcp = {name: None for name in _TCP_FIELDS}
            tcp["tcp_error"] = str(exc)
            tcp["tcp_bytes"] = None
        record = {
            "kind": "sample",
            "mono": mono,
            "wall": wall,
            "buf": buf,
            "snd_wnd": tcp.get("snd_wnd"),
            "snd_cwnd": tcp.get("snd_cwnd"),
            "snd_sbbytes": tcp.get("snd_sbbytes"),
            "txbytes": tcp.get("txbytes"),
            "txretransmitbytes": tcp.get("txretransmitbytes"),
        }
        if tcp.get("tcp_error"):
            record["tcp_error"] = tcp["tcp_error"]
        tcp_bytes = tcp.get("tcp_bytes")
        if tcp_bytes not in (None, DARWIN_TCP_CONNECTION_INFO_SIZE):
            record["tcp_bytes"] = tcp_bytes
        self._append(record)

    def _append(self, record: dict) -> None:
        with self._lock:
            if record.get("kind") == "sample":
                self._samples += 1
            self._records.append(record)


def _u32(buf: bytes, offset: int) -> int | None:
    if len(buf) < offset + 4:
        return None
    return int.from_bytes(buf[offset:offset + 4], "little")


def _u64(buf: bytes, offset: int) -> int | None:
    if len(buf) < offset + 8:
        return None
    return int.from_bytes(buf[offset:offset + 8], "little")


def _dumps(record: dict) -> str:
    return json.dumps(_round(record), separators=(",", ":"))


def _round(value):
    """Six decimal places is one microsecond, enough to line up with tcpdump."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    return value
