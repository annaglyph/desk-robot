"""Eyes: the camera stream, and a live-view page for the browser.

The robot sends one JPEG per binary frame (type byte 0x02). A Mac webcam can
feed the same slot while the robot is away (see webcam.py). We keep only the
newest frame from whichever source is selected. A tiny HTTP server (standard
library, its own thread) serves:

  /          the console page (brain/liveview.html): what Rocky sees and
             hears, plus controls for his head, face, voice, sleep and
             listening tuning
  /stream    multipart MJPEG — the newest frame, pushed as it changes
  /frame     the newest frame as a plain JPEG
  /status    JSON: fps, frame age, chip temperature, last transcript, and
             whatever main.py adds through `state_provider`
  /api/<x>   POST, JSON body: a control action, handed to `command_handler`
             (main.py). Only answers requests that carry the X-Rocky-Console
             header, which a cross-site page can't add without a CORS
             preflight we never grant.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config


class Eyes:
    def __init__(self) -> None:
        self.jpeg: bytes = b""          # newest raw frame from the live camera
        self.source = "robot"           # "robot" or "mac": whose frames are accepted
        self.frame_at = 0.0
        self.frame_seq = 0
        self.display: bytes = b""       # what the browser shows (annotated if a tracker runs)
        self.display_seq = 0
        self.has_annotator = False       # set by the tracker; then it publishes frames
        self.tracking_info: dict = {}
        self.frames = 0
        self._fps_window: list[float] = []
        self.temperature: float | None = None
        self.last_heard = ""
        self.last_said = ""
        # main.py plugs in: extra /status fields, and the handler for /api/<action>.
        self.state_provider: Callable[[], dict] | None = None
        self.command_handler: Callable[[str, dict], dict] | None = None
        self._cond = threading.Condition()

    # ── camera → server ─────────────────────────────────────────────────────
    def set_source(self, source: str) -> None:
        """Choose which camera's frames count. Drops the previous picture so
        a question can't be answered with the camera we just left."""
        with self._cond:
            if source == self.source:
                return
            self.source = source
            self.jpeg = b""
            self.display = b""
            self.frame_at = 0.0
            self._cond.notify_all()

    def push_frame(self, jpeg: bytes, *, source: str) -> None:
        """Keep `jpeg` if `source` is the camera currently selected."""
        if not jpeg:
            return
        now = time.time()
        with self._cond:
            if source != self.source:
                return
            self.jpeg = jpeg
            self.frame_at = now
            self.frame_seq += 1
            self.frames += 1
            self._fps_window.append(now)
            self._fps_window = [t for t in self._fps_window if now - t < 2.0]
            if not self.has_annotator:
                self.display = jpeg
                self.display_seq = self.frame_seq
            self._cond.notify_all()

    def current(self) -> tuple[bytes, str]:
        """The newest frame and which camera it came from."""
        with self._cond:
            return self.jpeg, self.source

    def publish(self, jpeg: bytes) -> None:
        """A frame for the browser (the tracker's annotated copy)."""
        if not jpeg:
            return
        with self._cond:
            self.display = jpeg
            self.display_seq += 1
            self._cond.notify_all()

    def wait_for_display(self, seen_seq: int, timeout: float = 1.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.display_seq != seen_seq, timeout=timeout)
            return self.display_seq

    def latest(self, max_age: float = 2.0) -> bytes | None:
        """The newest frame if it's recent enough to be worth showing the model."""
        if self.jpeg and time.time() - self.frame_at <= max_age:
            return self.jpeg
        return None

    def fps(self) -> float:
        return len(self._fps_window) / 2.0

    def wait_for_new(self, seen_seq: int, timeout: float = 1.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.frame_seq != seen_seq, timeout=timeout)
            return self.frame_seq

    # ── browser ─────────────────────────────────────────────────────────────
    def serve(self, port: int, bind: str = "127.0.0.1") -> None:
        eyes = self
        page = (Path(__file__).with_name("liveview.html").read_text(encoding="utf-8")
                .replace("{name}", config.ROBOT_NAME).encode())
        local_hosts = ("localhost", "127.0.0.1", "::1", bind.lower())

        class Handler(BaseHTTPRequestHandler):
            server_version = "desk-robot"  # don't advertise the Python version
            sys_version = ""

            def log_message(self, *args) -> None:  # keep the console quiet
                pass

            def _local(self) -> bool:
                # A malicious web page can point its own domain at 127.0.0.1
                # (DNS rebinding) and read a localhost server. Only answer
                # requests addressed to us by a local name.
                host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
                if host not in local_hosts:
                    self._reply(403, "text/plain", b"forbidden")
                    return False
                return True

            def do_POST(self) -> None:
                if not self._local():
                    return
                # Controls: a page on another site could still POST here
                # (browsers allow simple cross-site POSTs), so demand a custom
                # header — that turns it into a preflighted request, and we
                # never answer preflights. Belt and braces: check Origin too.
                origin = (self.headers.get("Origin") or "").lower()
                origin_host = origin.split("://", 1)[-1].split(":")[0].strip("[]")
                if self.headers.get("X-Rocky-Console") != "1" or (origin and origin_host not in local_hosts):
                    self._reply(403, "application/json", b'{"error": "not the console"}')
                    return
                if not self.path.startswith("/api/") or eyes.command_handler is None:
                    self._reply(404, "application/json", b'{"error": "no such control"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= length <= 64 * 1024:
                        raise ValueError("bad content length")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                except (ValueError, json.JSONDecodeError) as e:
                    self._reply(400, "application/json", json.dumps({"error": f"bad request: {e}"}).encode())
                    return
                action = self.path[len("/api/"):]
                try:
                    result = eyes.command_handler(action, payload)
                except ValueError as e:  # the control said no
                    self._reply(400, "application/json", json.dumps({"error": str(e)}).encode())
                    return
                except Exception as e:  # something broke inside the server
                    self._reply(500, "application/json", json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
                    return
                self._reply(200, "application/json", json.dumps({"ok": True, **(result or {})}).encode())

            def do_GET(self) -> None:
                if not self._local():
                    return
                if self.path == "/":
                    self._reply(200, "text/html; charset=utf-8", page)
                elif self.path == "/frame":
                    if eyes.jpeg:
                        self._reply(200, "image/jpeg", eyes.jpeg)
                    else:
                        self._reply(503, "text/plain", b"no frame yet")
                elif self.path == "/status":
                    st = {
                        "fps": round(eyes.fps(), 1),
                        "frame_age_s": round(time.time() - eyes.frame_at, 1) if eyes.frame_at else None,
                        "frames": eyes.frames,
                        "camera": eyes.source,
                        "temperature_c": eyes.temperature,
                        "last_heard": eyes.last_heard,
                        "last_said": eyes.last_said,
                        **eyes.tracking_info,
                    }
                    if eyes.state_provider is not None:
                        try:
                            st.update(eyes.state_provider())
                        except Exception as e:  # never let a status bug kill the page
                            st["state_error"] = str(e)
                    self._reply(200, "application/json", json.dumps(st).encode())
                elif self.path == "/stream":
                    self._stream()
                else:
                    self._reply(404, "text/plain", b"not found")

            def _reply(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")  # robot-supplied bytes stay images
                self.end_headers()
                self.wfile.write(body)

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                seq = -1
                try:
                    while True:
                        seq = eyes.wait_for_display(seq, timeout=1.0)
                        frame = eyes.display
                        if not frame:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer((bind, port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

