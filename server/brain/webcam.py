"""Mac webcam fallback for when the robot's camera isn't connected.

Frames land in Eyes the same way the robot's JPEGs do. The camera is opened
only while this source is selected, and closed again when the robot takes
over, so a desk webcam isn't held open for the whole session.

On macOS, OpenCV's AVFoundation backend opens cameras by index and does not
report names. The index order is the AVFoundation discovery session below,
so a CAMERA_DEVICE name can be turned back into that index.
"""

from __future__ import annotations

import sys
import threading
import time

from . import config
from .devices import builtin_camera, choose, match_name
from .eyes import Eyes

# Longest side of a frame handed to the brain. The robot's own camera is
# 320x240; this is larger so a desk webcam is actually useful for "what do
# you see?", and still small enough that face tracking stays cheap.
_LONG_SIDE = 640
_JPEG_QUALITY = 80


def resolve_camera(names: list[str], wanted: str | None) -> int | None:
    """Index of the one camera `wanted` names. None for `wanted` means the first."""
    if not wanted:
        return 0
    return match_name(names, wanted)


def select_camera(names: list[str], spec: str | None) -> tuple[int, str | None]:
    """Which camera to open, and which configured names were not present.

    Favourite name first, then the built-in camera, then the first listed
    camera when this Mac's own camera cannot be recognised.
    """
    index, missed = choose(names, spec, builtin_camera)
    if index is not None:
        return index, missed
    if names:
        builtin = builtin_camera(names)
        if builtin is not None:
            return builtin, missed
    return 0, missed


def camera_names() -> list[str]:
    """Names in OpenCV's index order. Empty when this platform can't list them."""
    if sys.platform != "darwin":
        return []
    try:
        return _avfoundation_names()
    except Exception:
        return []


class MacCamera:
    """Captures JPEGs from a local webcam into Eyes while it is the live source."""

    def __init__(self, eyes: Eyes) -> None:
        self.eyes = eyes
        self.name = ""
        self.names: list[str] = []
        self.notice: str | None = None  # a preferred camera was absent; who we used instead
        self.error: str | None = None
        self._live = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._live

    def start(self) -> None:
        """Open the camera and grab frames. Returns once it is open, or has failed."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._opened.clear()
            self.error = None
            self._thread = threading.Thread(target=self._run, name="mac-camera", daemon=True)
            self._thread.start()
            self._opened.wait(timeout=8)
        if self._stop.is_set():
            return  # the robot's camera took over while this one was opening
        if self.notice:
            print(f"({self.notice})")
        if self._live:
            print(f"seeing through Mac camera \"{self.name}\"")
        else:
            detail = self.error or "timed out opening it"
            print(f"(Mac camera unavailable: {detail})")
            print("  the robot's camera still works once it connects; for the Mac fallback")
            print("  allow this app under System Settings > Privacy & Security > Camera")
            if self.names:
                listed = ", ".join(f"[{i}] {n}" for i, n in enumerate(self.names))
                print(f"  cameras: {listed}")

    def stop(self) -> None:
        """Close the camera. The indicator light goes out with it."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        cap = None
        try:
            cap, self.name, self.names, self.error, self.notice = _open()
            if cap is not None and self._stop.is_set():
                cap.release()
                cap = None
            self._live = cap is not None
        finally:
            self._opened.set()
        if cap is None:
            return
        try:
            _grab(cap, self.eyes, self._stop)
        except Exception as e:
            self.error = str(e)
            print(f"(Mac camera stopped: {e})")
        finally:
            self._live = False
            cap.release()


def _open():
    """Open the preferred camera that is actually plugged in.

    Returns (capture or None, name, all names, error or None, notice or None).
    """
    import cv2

    names = camera_names()
    index, missed = select_camera(names, config.CAMERA_DEVICE)
    name = names[index] if index < len(names) else f"camera {index}"
    notice = f"{missed} isn't connected — using \"{name}\"" if missed else None
    backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
    try:
        cap = cv2.VideoCapture(index, backend)
    except cv2.error as e:
        return None, name, names, str(e), notice
    if not cap.isOpened():
        cap.release()
        return None, name, names, f"could not open \"{name}\"", notice
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, _LONG_SIDE)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(_LONG_SIDE * 3 / 4))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap, name, names, None, notice


def _grab(cap, eyes: Eyes, stop: threading.Event) -> None:
    import cv2

    interval = 1.0 / max(config.CAMERA_FPS, 0.5)
    warmup_until = time.time() + 0.4  # the first frames out of a USB camera are often black
    next_at = 0.0
    while not stop.is_set():
        ok, frame = cap.read()
        if not ok or frame is None:
            print("(Mac camera stopped delivering frames)")
            return
        now = time.time()
        if now < warmup_until or now < next_at:
            continue
        next_at = now + interval
        jpeg = _encode(cv2, frame)
        if jpeg:
            eyes.push_frame(jpeg, source="mac")


def _encode(cv2, frame) -> bytes | None:
    height, width = frame.shape[:2]
    long_side = max(height, width)
    if long_side > _LONG_SIDE:
        scale = _LONG_SIDE / long_side
        frame = cv2.resize(
            frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
        )
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY])
    return buf.tobytes() if ok else None


def _avfoundation_names() -> list[str]:
    import ctypes

    libobjc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    libobjc.objc_getClass.restype = ctypes.c_void_p
    libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
    libobjc.sel_registerName.restype = ctypes.c_void_p
    libobjc.sel_registerName.argtypes = [ctypes.c_char_p]

    def msg(obj, sel: bytes, *args, restype=ctypes.c_void_p, argtypes=()):
        types = (ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        send = ctypes.CFUNCTYPE(restype, *types)((b"objc_msgSend", libobjc))
        return send(obj, libobjc.sel_registerName(sel), *args)

    ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    avf = ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")

    def symbol(name: str):
        return ctypes.c_void_p.in_dll(avf, name).value

    type_names = ["AVCaptureDeviceTypeBuiltInWideAngleCamera"]
    if _has_symbol(avf, "AVCaptureDeviceTypeExternal"):
        type_names.append("AVCaptureDeviceTypeExternal")
    elif _has_symbol(avf, "AVCaptureDeviceTypeExternalUnknown"):
        type_names.append("AVCaptureDeviceTypeExternalUnknown")
    for extra in ("AVCaptureDeviceTypeDeskViewCamera", "AVCaptureDeviceTypeContinuityCamera"):
        if _has_symbol(avf, extra):
            type_names.append(extra)

    pool = msg(libobjc.objc_getClass(b"NSAutoreleasePool"), b"new")
    try:
        type_ids = [symbol(name) for name in type_names]
        buf = (ctypes.c_void_p * len(type_ids))(*type_ids)
        types = msg(
            libobjc.objc_getClass(b"NSArray"),
            b"arrayWithObjects:count:",
            buf,
            len(type_ids),
            argtypes=(ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong),
        )
        session = msg(
            libobjc.objc_getClass(b"AVCaptureDeviceDiscoverySession"),
            b"discoverySessionWithDeviceTypes:mediaType:position:",
            types,
            symbol("AVMediaTypeVideo"),
            0,
            argtypes=(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long),
        )
        devices = msg(session, b"devices")
        count = msg(devices, b"count", restype=ctypes.c_ulong)
        names = []
        for i in range(count):
            device = msg(devices, b"objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
            raw = msg(msg(device, b"localizedName"), b"UTF8String", restype=ctypes.c_char_p)
            names.append(raw.decode())
        return names
    finally:
        if pool:
            msg(pool, b"drain")


def _has_symbol(lib, name: str) -> bool:
    import ctypes
    try:
        ctypes.c_void_p.in_dll(lib, name)
    except ValueError:
        return False
    return True
