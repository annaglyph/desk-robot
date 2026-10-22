"""The authenticated robot socket disables Nagle and reports the result."""

import json
import os
import socket
import unittest
from unittest.mock import patch

import websockets

from brain import main


class TcpNoDelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_socket_disables_nagle(self):
        token = "nodelay-test"
        previous_source = main.eyes.source
        main.eyes.source = "mac"
        main.robot_socket = None
        server = await websockets.serve(main.handle_robot, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with patch.dict(os.environ, {"ROBOT_TOKEN": token}), \
                    patch.object(main, "pick_mic_source"), \
                    patch.object(main, "pick_camera_source"), \
                    patch.object(main.config, "MIC_SOURCE", "off"):
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.send(json.dumps({
                        "type": "hello", "who": "desk-robot", "fw": "test", "token": token,
                    }))
                    reply = await ws.recv()
                    self.assertIn("glance", reply)
                    sock = main.robot_socket.transport.get_extra_info("socket")
                    # macOS reads a disabled Nagle algorithm back as 4; Linux as 1.
                    self.assertNotEqual(
                        sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 0)
        finally:
            server.close()
            await server.wait_closed()
            main.robot_socket = None
            main.eyes.source = previous_source

    def test_missing_socket_is_reported(self):
        class Dead:
            transport = None

        self.assertIsNone(main.enable_tcp_nodelay(Dead()))
