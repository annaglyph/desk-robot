"""desk-robot brain server.

Run with:  python -m brain.main   (from the server/ directory, venv active)

Three jobs:
  1. WebSocket server the robot connects to over WiFi.
  2. An interactive console so you can talk to the brain and puppet the
     robot from your keyboard right now.
  3. Listening: hears "hey Rocky" on the mic, sends what you say to
     the language model, and speaks the reply through the robot (or the Mac).

A reply is a pipeline, not a wait: each model round is read completely,
then its sentences go to the voice, and the voice's audio streams to the
speaker as it's made. A round that calls a tool is not spoken. If you start
talking again before Rocky has begun speaking, the reply is dropped and he
listens to the rest of what you're saying, then answers once.

Console commands:
  ask <question>   send a question to the brain, print and speak the reply
  say <text>       speak text (robot speaker if connected, else the Mac)
  volume <0-1>     set the robot's speaker volume
  track on|off     face tracking (head follows you)
  listen           toggle listening on the microphone
  mic              3-second level meter to check the microphone
  emo <name>       push a face to the connected robot
  pan <deg>        push a head turn to the connected robot
  tilt <deg>       push a head nod to the connected robot
  status           show whether a robot is connected
  quit
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import sys
import threading
import time

import websockets

from . import config, mouth, personality
from .thinking import Interrupted, RobotBrain
from .ears import Ears, normalize, strip_wake_word
from .eyes import Eyes
from .tracker import Tracker
from .webcam import MacCamera

robot_socket: websockets.ServerConnection | None = None
brain: RobotBrain | None = None  # created in main() once the event loop exists
ears: Ears | None = None
# (text, when speech began, when it ended, when the transcript was ready)
heard: asyncio.Queue[tuple[str, float, float, float]] = asyncio.Queue()
speech_started = asyncio.Event()  # the human began a new turn (set by the ears)
awake_until = 0.0  # while time.time() < this, he's awake: no wake word needed
robot_speak_done = asyncio.Event()  # robot finished playing the last reply
eyes = Eyes()
mac_camera = MacCamera(eyes)
head_moves: asyncio.Queue[tuple[float | None, float | None, bool]] = asyncio.Queue()
tracker: Tracker | None = None

FRAME_BYTES = 3200  # 100 ms of 16 kHz s16le per audio frame to the robot

# What the live-view console shows and can change (see console_state / console_command).
current_emotion = "neutral"
speaker_volume = 0.7        # mirrors firmware SPEAKER_VOLUME until changed here
thinking = False            # a reply is being composed (before it's spoken)
TUNABLE = {                 # config knobs the console may change live: (min, max)
    "VAD_THRESHOLD": (0.1, 0.95),
    "TURN_THRESHOLD": (0.1, 0.95),
    "TURN_MAX_SILENCE": (0.5, 6.0),
    "AWAKE_SECONDS": (10.0, 600.0),
}
VOICE_TUNABLE = {           # speaker tuning the console may change live (mouth.Leveler reads these per reply)
    "TTS_LEVEL": (0.08, 0.40),
    "TTS_HIGHPASS_HZ": (0.0, 600.0),
    "TTS_PRESENCE_DB": (0.0, 12.0),
}


async def send_to_robot(payload: dict) -> bool:
    global current_emotion
    if payload.get("type") == "emotion":
        current_emotion = payload.get("name", current_emotion)
    if robot_socket is None:
        print("(no robot connected — command not sent)")
        return False
    await robot_socket.send(json.dumps(payload))
    return True


def pick_mic_source() -> None:
    """auto: the robot's mic while it's connected, the Mac's otherwise."""
    if ears is None:
        return
    if config.MIC_SOURCE == "auto":
        source = "robot" if robot_socket is not None else "mac"
    else:
        source = config.MIC_SOURCE
    if source != ears.source:
        ears.set_source(source)
        if source == "mac" and not ears.has_mac_mic:
            print(f"(no Mac mic: {ears.mac_error} — nothing is listening until the robot connects)")
        else:
            print(f"listening through the {'robot' if source == 'robot' else 'Mac'} mic")


def pick_camera_source() -> None:
    """auto: the robot's camera while it's connected, the Mac's webcam otherwise.

    The webcam is closed whenever the robot's camera is selected, so it isn't
    held open (and its light left on) for the whole session.
    """
    if config.CAMERA_SOURCE == "auto":
        source = "robot" if robot_socket is not None else "mac"
    elif config.CAMERA_SOURCE in ("robot", "mac"):
        source = config.CAMERA_SOURCE
    else:
        print(f"(CAMERA_SOURCE {config.CAMERA_SOURCE!r} is not auto, robot, or mac — using auto)")
        source = "robot" if robot_socket is not None else "mac"
    if source == eyes.source and (source != "mac" or mac_camera.running):
        return
    eyes.set_source(source)
    if source == "mac":
        mac_camera.start()
    else:
        mac_camera.stop()
        if robot_socket is not None:
            print("seeing through the robot camera")


async def handle_robot(websocket: websockets.ServerConnection) -> None:
    global robot_socket
    peer = websocket.remote_address[0] if websocket.remote_address else "?"
    # Anyone on the WiFi can reach this port, so the first message must be a
    # hello carrying the shared token from server/.env. Anything else is
    # dropped without a reply.
    try:
        first = await asyncio.wait_for(websocket.recv(), timeout=2)
        hello = json.loads(first) if isinstance(first, str) else {}
    except (asyncio.TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
        hello = {}
    if not isinstance(hello, dict):
        hello = {}  # "[1]", "42", "null" are valid JSON but not a hello
    expected = os.environ.get("ROBOT_TOKEN", "")
    if not expected:
        print("(refusing robot: set ROBOT_TOKEN in server/.env and firmware/include/secrets.h)")
        await websocket.close(1008)
        return
    offered = str(hello.get("token", "")).encode()  # bytes: compare_digest rejects non-ASCII str
    if hello.get("type") != "hello" or not secrets.compare_digest(offered, expected.encode()):
        print(f"(refused a connection from {peer}: bad or missing token)")
        await websocket.close(1008)
        return
    if robot_socket is not None:
        print(f"(refused a second robot from {peer}: one is already connected)")
        await websocket.close(1013)
        return
    robot_socket = websocket
    print(f"robot connected! (fw {hello.get('fw', '?')}, {peer})")
    pick_mic_source()
    pick_camera_source()
    if config.MIC_SOURCE in ("auto", "robot"):
        await send_to_robot({"type": "mic", "on": True})
    if eyes.source == "robot":
        await send_to_robot({"type": "stream", "on": True, "fps": config.CAMERA_FPS})
    # No idle head glances: they fight deliberate looks. The eyes still move.
    await send_to_robot({"type": "glance", "on": False})
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                kind = message[:1]
                if kind == b"\x01" and ears is not None and len(message) % 2 == 1:
                    ears.push_audio(message[1:])  # 1 type byte + whole 16-bit samples
                elif kind == b"\x02":
                    eyes.push_frame(message[1:], source="robot")
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "state":
                continue  # heartbeat every 5 s; not worth the console space
            if event.get("type") == "speak_done":
                robot_speak_done.set()
                continue
            if event.get("type") == "temp":
                try:
                    eyes.temperature = float(event.get("c"))
                except (TypeError, ValueError):
                    pass  # not a number; leave the last reading
                continue
            print(f"robot: {event}")
    except websockets.ConnectionClosed:
        pass
    finally:
        # Only clear the slot if it's still ours: when the robot reboots, the
        # new connection can arrive before the old one is noticed as dead.
        if robot_socket is websocket:
            robot_socket = None
            print("robot disconnected")
            pick_mic_source()
            pick_camera_source()


async def handle_console_line(line: str) -> bool:
    """Returns False when the server should shut down."""
    line = line.strip()
    if not line:
        return True
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()

    if cmd == "quit":
        return False
    if cmd == "status":
        print("robot connected" if robot_socket else "no robot connected")
        if eyes.source == "mac":
            print(f"camera: {mac_camera.name or mac_camera.error or 'Mac camera'}")
        else:
            print("camera: robot")
    elif cmd == "emo":
        if arg in config.EMOTIONS:
            await send_to_robot({"type": "emotion", "name": arg})
        else:
            print(f"emotions: {', '.join(config.EMOTIONS)}")
    elif cmd in ("pan", "tilt"):
        try:
            deg = float(arg)
            await send_to_robot({"type": cmd, "deg": deg})
            if tracker is not None:
                tracker.note_pose(**{cmd: deg})
        except ValueError:
            print(f"usage: {cmd} <degrees>")
    elif cmd == "track":
        await set_tracking(arg != "off")
    elif cmd == "ask":
        if not arg:
            print("usage: ask <question>")
            return True
        await converse(arg)
    elif cmd == "listen":
        if ears is None:
            await start_listening()
        else:
            stop_listening()
    elif cmd == "mic":
        await mic_meter()
    elif cmd == "say":
        if arg:
            await say(arg)
        else:
            print("usage: say <text>")
    elif cmd == "volume":
        try:
            await set_volume(float(arg))
        except ValueError:
            print("usage: volume <0.0-1.0>")
    else:
        print("commands: ask <q> | say <text> | volume <0-1> | listen | mic | track on|off | emo <name> | pan <deg> | tilt <deg> | status | quit")
    return True


# ── Rocky's abilities (called by the brain, from its worker thread) ──────────

# After the move is commanded, give the servo this long to arrive, then wait
# for a frame newer than the pre-move one. Together they stay within the old
# worst case: a fixed 1.2 s plus up to 1.0 s of polling.
LOOK_SETTLE_SECONDS = 0.6
LOOK_WAIT_BUDGET = 2.2


async def _wait_for_look_frame(seen_seq: int) -> None:
    """Settle, then wait for a frame newer than `seen_seq`. Never past the budget."""
    started = time.monotonic()
    await asyncio.sleep(min(LOOK_SETTLE_SECONDS, LOOK_WAIT_BUDGET))
    remaining = LOOK_WAIT_BUDGET - (time.monotonic() - started)
    if remaining <= 0:
        return
    seen = seen_seq
    timeout = remaining
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: eyes.wait_for_new(seen, timeout)
    )


def _look_image(seen_seq: int) -> tuple[str, bytes | None]:
    """What to tell the model about the picture after a head move.

    A frame that arrived after the command is fresh. An older in-date frame
    is still attached, and said to be older. Anything else is no image.
    """
    jpeg = eyes.latest()
    if eyes.frame_seq != seen_seq and jpeg is not None:
        return " Fresh camera image attached.", jpeg
    if jpeg is not None:
        return (
            " No new camera frame after the move. The attached image is an older one, not a new view.",
            jpeg,
        )
    return " No camera image available.", None


async def look(args: dict) -> tuple[str, bytes | None]:
    """Move the head, wait for it to get there, and grab a fresh frame.
    Only the axis that was asked for moves: "left"/"right" pan, "down"/
    "level" tilt, "center" both."""
    if eyes.source != "robot":
        # The computer's camera stays put, so a head move wouldn't change the picture.
        jpeg = eyes.latest()
        note = "The picture is from the computer's camera, which does not move. No head was turned."
        return (note + (" Fresh camera image attached." if jpeg else " No camera image available."), jpeg)
    pan = tracker.pan if tracker else 0.0
    tilt = tracker.tilt if tracker else 0.0
    move_pan = move_tilt = False
    d = args.get("direction")
    try:
        amount = float(args["degrees"]) if args.get("degrees") is not None else None
    except (TypeError, ValueError):
        amount = None
    if d in ("left", "right"):
        deg = amount if amount is not None else 40.0
        pan, move_pan = (-deg if d == "left" else deg), True
    elif d == "down":
        tilt, move_tilt = -(amount if amount is not None else 30.0), True  # a normal glance down; ask for degrees to go further
    elif d == "level":
        tilt, move_tilt = config.TRACK_TILT_MAX, True
    elif d == "center":
        pan, tilt, move_pan, move_tilt = 0.0, config.TRACK_TILT_MAX, True, True
    if not (move_pan or move_tilt):
        return ("Say where to look: left, right, down, level, or center.", None)
    pan = max(-config.TRACK_PAN_LIMIT, min(config.TRACK_PAN_LIMIT, pan))
    tilt = max(config.TRACK_TILT_MIN, min(config.TRACK_TILT_MAX, tilt))

    if tracker is not None and tracker.enabled:
        await set_tracking(False, announce=False)  # tracking would drag the head back
    await set_head_held(d != "center")
    seen = eyes.frame_seq  # the picture from before this move is commanded
    if move_pan:
        await send_to_robot({"type": "pan", "deg": pan})
    if move_tilt:
        await send_to_robot({"type": "tilt", "deg": tilt})
    if tracker is not None:
        tracker.note_pose(pan=pan if move_pan else None, tilt=tilt if move_tilt else None)
    await _wait_for_look_frame(seen)
    note, jpeg = _look_image(seen)
    pan_word = "left" if pan < -5 else "right" if pan > 5 else "center"
    if tilt <= config.TRACK_TILT_MIN + 0.5:
        tilt_word = "down, as far as it goes"
    elif tilt >= config.TRACK_TILT_MAX - 0.5:
        tilt_word = "level, as high as it goes"
    else:
        tilt_word = "down" if tilt < -5 else "level"
    where = f"Head is now at pan {pan:.0f} deg ({pan_word}), tilt {tilt:.0f} deg ({tilt_word})."
    return (where + note, jpeg)


head_held = False  # he was told to look somewhere and is holding that pose


async def set_head_held(held: bool) -> None:
    """Remember that a deliberate look is in effect (shown on the console).
    Idle glances are always off, so nothing else needs to move."""
    global head_held
    head_held = held


async def set_tracking(on: bool, announce: bool = True) -> tuple[str, bytes | None]:
    if tracker is None:
        return ("no tracker running", None)
    tracker.enabled = on
    if not on and tracker.tracking:
        tracker.tracking = False
        eyes.tracking_info = {"tracking": False, "pan": tracker.pan, "tilt": tracker.tilt}
    if announce:
        print(f"tracking {'on' if on else 'off'}")
    return ("now following the human's face" if on else "stopped following", None)


def _sync(coro_fn):
    """Wrap an async ability so the brain's worker thread can call it."""
    def run(args: dict):
        return asyncio.run_coroutine_threadsafe(coro_fn(args), main_loop).result(timeout=15)
    return run


main_loop: asyncio.AbstractEventLoop | None = None
ABILITIES = {
    "look": _sync(look),
    "track_face": _sync(lambda args: set_tracking(bool(args.get("on", True)))),
}


def wants_camera(question: str) -> bool:
    """Does the question sound like it's about what Rocky can see?"""
    q = " " + normalize(question) + " "
    return any(f" {w} " in q or (" " in w and w in q) for w in config.CAMERA_WORDS)


# ── Speaking ─────────────────────────────────────────────────────────────────

class Timeline:
    """Stage times of one reply, measured from the end of the human's speech."""

    ORDER = ("heard", "face", "first sentence", "first audio", "speaking", "done")

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.time())

    def report(self) -> None:
        parts = [f"{n} {self.marks[n] - self.t0:.2f}s" for n in self.ORDER if n in self.marks]
        if parts:
            print("  timing (after your last word): " + " · ".join(parts))


class SpokenReply:
    """One reply in flight, as a three-stage pipeline:
    brain thread (model → sentences) → voice thread (Fish → PCM) → the speaker.

    Each stage hands off through a queue, so the first sentence is being
    voiced while the model writes the second, and audio plays while the
    voice is still generating."""

    BREAK = b""  # marks a sentence boundary in the audio queue

    def __init__(self, loop: asyncio.AbstractEventLoop, timeline: Timeline) -> None:
        self.loop = loop
        self.tl = timeline
        self.leveler = mouth.Leveler()  # one per reply: steady loudness across its sentences
        self.sentences: queue.Queue[str | None] = queue.Queue()
        self.audio: queue.Queue[bytes | None] = queue.Queue()
        self.cancel = threading.Event()
        self.spoken: list[str] = []
        self.pcm: list[bytes] = []

    @property
    def text(self) -> str:
        return " ".join(self.spoken)

    def think_and_speak(self, question: str, jpeg: bytes | None, camera_wanted: bool = False) -> None:
        threading.Thread(target=self._think, args=(question, jpeg, camera_wanted), daemon=True).start()
        threading.Thread(target=self._voice, daemon=True).start()

    def speak_fixed(self, text: str) -> None:
        """A canned line: no brain involved."""
        self.spoken.append(text)
        self.sentences.put(text)
        self.sentences.put(None)
        threading.Thread(target=self._voice, daemon=True).start()

    def _on_emotion(self, name: str) -> None:
        # Called from the brain thread as soon as the reply's emotion is known.
        if self.cancel.is_set():
            return
        self.tl.mark("face")
        asyncio.run_coroutine_threadsafe(send_to_robot({"type": "emotion", "name": name}), self.loop)

    def _think(self, question: str, jpeg: bytes | None, camera_wanted: bool) -> None:
        gen = brain.reply(question, jpeg, on_emotion=self._on_emotion, cancelled=self.cancel,
                          camera_wanted=camera_wanted)
        try:
            for sentence in gen:
                if self.cancel.is_set():
                    break
                if not self.spoken:
                    self.tl.mark("first sentence")
                self.spoken.append(sentence)
                print(f"{config.ROBOT_NAME} [{brain.emotion}]: {sentence}")
                self.sentences.put(sentence)
        except Interrupted:
            pass
        except Exception as e:  # never let a brain hiccup kill the server
            print(f"(brain error: {e})")
        finally:
            gen.close()  # drops the question from his memory if we bailed early
            self.sentences.put(None)

    def _voice(self) -> None:
        try:
            while (sentence := self.sentences.get()) is not None:
                if self.cancel.is_set():
                    continue
                for chunk in mouth.stream(sentence, self.leveler):
                    if self.cancel.is_set():
                        break
                    if not self.pcm:
                        self.tl.mark("first audio")
                    self.pcm.append(chunk)
                    self.audio.put(chunk)
                self.audio.put(self.BREAK)
        except Exception as e:
            print(f"(voice error: {e})")
        finally:
            self.audio.put(None)

    async def _next(self) -> bytes | None:
        return await self.loop.run_in_executor(None, self.audio.get)

    async def play(self, interruptible: bool = True) -> bool:
        """Send the audio to the speaker as it arrives. Until the first audio
        is ready the human can cancel the whole reply by talking again;
        returns False in that case. Once Rocky is speaking the mic is muted
        (his voice would trigger it) and he finishes what he's saying."""
        get = asyncio.ensure_future(self._next())
        while not get.done():
            if interruptible and (speech_started.is_set() or (ears is not None and ears.talking)):
                self._abort()
                await get
                return False
            await asyncio.wait({get}, timeout=0.05)
        first = get.result()
        if first is None:
            return True  # nothing to say
        if ears is not None:
            ears.muted.set()
        try:
            if robot_socket is not None:
                await self._play_robot(first)
            else:
                await self._play_mac(first)
        finally:
            if ears is not None:
                ears.muted.clear()
        self.tl.mark("done")
        return True

    def _abort(self) -> None:
        self.cancel.set()
        brain.abandon()
        self.sentences.put(None)  # wake the voice thread so it can exit
        self.audio.put(None)

    async def _play_robot(self, first: bytes) -> None:
        """Stream PCM to the robot (docs/protocol.md) and wait until it has played."""
        robot_speak_done.clear()
        # bytes 0 = length unknown: the robot starts after 200 ms of buffer.
        await send_to_robot({"type": "speak_begin", "bytes": 0})
        self.tl.mark("speaking")
        buf = bytearray()
        total = 0
        chunk: bytes | None = first
        while chunk is not None:
            buf += chunk
            while len(buf) >= FRAME_BYTES:
                if robot_socket is None:
                    return
                await robot_socket.send(b"\x01" + bytes(buf[:FRAME_BYTES]))
                del buf[:FRAME_BYTES]
                total += FRAME_BYTES
            chunk = await self._next()
        if buf and robot_socket is not None:
            await robot_socket.send(b"\x01" + bytes(buf))
            total += len(buf)
        await send_to_robot({"type": "speak_end"})
        try:
            await asyncio.wait_for(robot_speak_done.wait(), timeout=total / 32000 + 5)
        except asyncio.TimeoutError:
            print("(robot never said it finished speaking)")

    async def _play_mac(self, first: bytes) -> None:
        """No robot: play each sentence on the Mac as soon as it's complete."""
        self.tl.mark("speaking")
        buf = bytearray()
        chunk: bytes | None = first
        while chunk is not None:
            if chunk == self.BREAK:
                if buf:
                    await self.loop.run_in_executor(None, mouth.play, bytes(buf))
                    buf = bytearray()
            else:
                buf += chunk
            chunk = await self._next()
        if buf:
            await self.loop.run_in_executor(None, mouth.play, bytes(buf))


async def converse(question: str, ended_at: float | None = None, heard_at: float | None = None) -> bool:
    """Ask the brain, show the face, and speak the answer as it forms.
    Returns False if the human started talking again before Rocky spoke
    (the reply was dropped and the question is still open)."""
    global thinking
    loop = asyncio.get_running_loop()
    tl = Timeline(ended_at or time.time())
    if heard_at is not None:
        tl.marks["heard"] = heard_at
    speech_started.clear()
    await send_to_robot({"type": "emotion", "name": "thinking"})
    camera_wanted = config.SEND_CAMERA_TO_BRAIN and wants_camera(question)
    jpeg = eyes.latest() if camera_wanted else None
    if camera_wanted and jpeg is None:
        print("  (camera has no fresh frame — telling him he can't see right now)")
    reply = SpokenReply(loop, tl)
    reply.think_and_speak(question, jpeg, camera_wanted)
    thinking = True
    try:
        finished = await reply.play()
    except Exception as e:  # a speaker hiccup shouldn't kill the server
        print(f"(could not speak: {e})")
        finished = True
    if finished and reply.text:
        eyes.last_said = reply.text
        if config.DEBUG_TTS_CHECK and reply.pcm:
            loop.run_in_executor(None, check_tts, reply.text, b"".join(reply.pcm))
    finish_turn(finished)  # refresh the clock before thinking clears
    if not finished:
        print("  (you kept talking — Rocky will hear the rest and answer once)")
        await send_to_robot({"type": "emotion", "name": "neutral"})
        return False
    tl.report()
    return True


async def say(text: str) -> bytes:
    """Speak a fixed line: through the robot's speaker when it's connected,
    else the Mac. Ears are muted meanwhile so Rocky doesn't hear himself."""
    reply = SpokenReply(asyncio.get_running_loop(), Timeline(time.time()))
    reply.speak_fixed(text)
    try:
        await reply.play(interruptible=False)
    except Exception as e:
        print(f"(could not speak: {e})")
    return b"".join(reply.pcm)


_tts_checker = None


def check_tts(text: str, pcm: bytes) -> None:
    """Transcribe what the voice model produced and compare it with the text
    it was given. Extra words = the model hallucinated (a laugh, "hello", a
    sound). Runs in a worker thread; prints only when something's off."""
    global _tts_checker
    import re
    from .ears import Transcriber
    if _tts_checker is None:
        _tts_checker = Transcriber("base.en")
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    heard_text = _tts_checker.transcribe(audio)
    words = lambda t: set(re.findall(r"[a-z']+", t.lower()))
    said, got = words(mouth.clean_for_tts(text)), words(heard_text)
    extra = got - said
    missing = said - got
    # Transcription is imperfect: only shout when several words are foreign.
    if len(extra) >= 3 or (len(extra) >= 2 and len(extra) >= len(got) / 3):
        print(f"  !! TTS CHECK: audio contains words not in the text: {sorted(extra)}")
        print(f"     text : {text}\n     heard: {heard_text}")
    elif len(missing) >= max(3, len(said) // 2):
        print(f"  !! TTS CHECK: audio is missing much of the text. heard: {heard_text}")


async def set_volume(level: float) -> None:
    global speaker_volume
    speaker_volume = max(0.0, min(1.0, level))
    await send_to_robot({"type": "volume", "level": speaker_volume})


# ── The live-view console (http://localhost:8766) ────────────────────────────

def console_state() -> dict:
    """Extra fields for /status: everything the page shows beyond the camera."""
    now = time.time()
    return {
        "robot": robot_socket is not None,
        "camera_name": mac_camera.name if eyes.source == "mac" else "",
        "listening": ears is not None,
        "mic": ears.source if ears is not None else None,
        "level": ears.level if ears is not None else 0.0,
        "speech_prob": ears.speech_prob if ears is not None else 0.0,
        "hearing": ears.hearing if ears is not None else False,
        "speaking": ears.muted.is_set() if ears is not None else False,
        "thinking": thinking,
        "awake": now < awake_until,
        "awake_for": max(0.0, awake_until - now),
        "emotion": current_emotion,
        "voice": {k: getattr(config, k) for k in VOICE_TUNABLE},
        "head_held": head_held,
        "tracking_enabled": tracker.enabled if tracker is not None else False,
        "volume": speaker_volume,
        "tuning": {k: getattr(config, k) for k in TUNABLE},
    }


def console_command(action: str, payload: dict) -> dict:
    """A control from the page. Runs on the HTTP thread; hops onto the event
    loop. Raises ValueError for anything the page shouldn't have asked."""
    fut = asyncio.run_coroutine_threadsafe(_console_command(action, payload), main_loop)
    return fut.result(timeout=10)


def _number(payload: dict, key: str, lo: float, hi: float) -> float:
    try:
        v = float(payload[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not (lo <= v <= hi):
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return v


async def _console_command(action: str, payload: dict) -> dict:
    global awake_until
    if action == "emotion":
        name = payload.get("name")
        if name not in config.EMOTIONS:
            raise ValueError(f"emotions: {', '.join(config.EMOTIONS)}")
        await send_to_robot({"type": "emotion", "name": name})
    elif action == "head":
        pan = _number(payload, "pan", -config.TRACK_PAN_LIMIT, config.TRACK_PAN_LIMIT)
        tilt = _number(payload, "tilt", config.TRACK_TILT_MIN, config.TRACK_TILT_MAX)
        if tracker is not None and tracker.enabled:
            await set_tracking(False)
        await set_head_held(True)
        await send_to_robot({"type": "pan", "deg": pan})
        await send_to_robot({"type": "tilt", "deg": tilt})
        if tracker is not None:
            tracker.note_pose(pan=pan, tilt=tilt)
    elif action == "center":
        await set_head_held(False)
        await send_to_robot({"type": "pan", "deg": 0})
        await send_to_robot({"type": "tilt", "deg": 0})
        if tracker is not None:
            tracker.note_pose(pan=0, tilt=0)
    elif action == "volume":
        await set_volume(_number(payload, "level", 0.0, 1.0))
    elif action == "track":
        await set_tracking(bool(payload.get("on", True)))
    elif action == "sleep":
        if payload.get("on", True):
            awake_until = 0.0
            await send_to_robot({"type": "emotion", "name": "sleepy"})
            await send_to_robot({"type": "asleep", "on": True})
            await set_head_held(False)
        else:
            awake_until = time.time() + config.AWAKE_SECONDS
            await send_to_robot({"type": "asleep", "on": False})
            await send_to_robot({"type": "emotion", "name": "neutral"})
    elif action in ("say", "ask"):
        text = str(payload.get("text", "")).strip()
        if not text or len(text) > 300:
            raise ValueError("text must be 1 to 300 characters")
        print(f"console: {action} {text}")
        asyncio.create_task(say(text) if action == "say" else converse(text))
    elif action == "tune":
        key = payload.get("key")
        if key not in TUNABLE:
            raise ValueError(f"tunable: {', '.join(TUNABLE)}")
        value = _number(payload, "value", *TUNABLE[key])
        setattr(config, key, value)
        if ears is not None:
            ears.apply_config()
        print(f"console: {key} = {value:g} (until restart; set it in config.py to keep)")
    elif action == "voice":
        # Speaker tuning; takes effect on the next reply.
        key = payload.get("key")
        if key not in VOICE_TUNABLE:
            raise ValueError(f"voice settings: {', '.join(VOICE_TUNABLE)}")
        value = _number(payload, "value", *VOICE_TUNABLE[key])
        setattr(config, key, value)
        print(f"console: {key} = {value:g} (until restart; set it in config.py to keep)")
    else:
        raise ValueError(f"no such control: {action}")
    return {}


# ── Listening ────────────────────────────────────────────────────────────────

async def mic_meter() -> None:
    """Three seconds of live mic level and speech probability, so you can see
    whether it hears you."""
    if ears is None:
        print("(not listening — type `listen` first)")
        return
    which = "robot mic" if ears.source == "robot" else f"Mac mic \"{ears.device_name}\""
    print(f"{which}: talk for 3 seconds...")
    peak_level = 0.0
    peak_prob = 0.0
    for _ in range(12):
        await asyncio.sleep(0.25)
        level, prob = ears.level, ears.speech_prob
        peak_level = max(peak_level, level)
        peak_prob = max(peak_prob, prob)
        print(f"  level {level:.3f} {'#' * min(40, int(level * 500)):40s} speech {prob:.2f}")
    print(f"peak level {peak_level:.3f}, peak speech probability {peak_prob:.2f} (needs > {config.VAD_THRESHOLD}) ->",
          "speech detected" if peak_prob > config.VAD_THRESHOLD else
          ("NOT DETECTED: raise MIC_GAIN in firmware config.h, or get closer" if ears.source == "robot" else
           "NOT DETECTED: wrong mic, gain down, or no mic permission"))
    if ears.source == "mac":
        import sounddevice as sd
        print("other inputs:", ", ".join(f"[{i}] {d['name']}" for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0))


async def start_listening() -> None:
    global ears
    loop = asyncio.get_running_loop()
    print(f"loading speech model {config.STT_MODEL} and the turn-taking models (first run downloads them)...")
    try:
        ears = await loop.run_in_executor(
            None,
            lambda: Ears(
                on_utterance=lambda text, t0, t1: loop.call_soon_threadsafe(
                    heard.put_nowait, (text, t0, t1, time.time())
                ),
                on_speech_start=lambda: loop.call_soon_threadsafe(speech_started.set),
            ),
        )
        mic = await loop.run_in_executor(None, ears.start)
    except Exception as e:
        ears = None
        print(f"(could not start listening: {e})")
        print("  check: mic plugged in? Is your terminal allowed to use the microphone (macOS:")
        print("  System Settings > Privacy & Security > Microphone)? MIC_DEVICE in server/.env?")
        return
    if ears.mac_error:
        print(f"(Mac mic unavailable: {ears.mac_error})")
        print("  the robot's mic still works; for a Mac fallback check MIC_DEVICE in server/.env,")
        print("  plug the interface in, or allow the Terminal to use the microphone")
    else:
        print(f"listening on \"{mic}\" — say \"hey {config.ROBOT_NAME}\"")
    pick_mic_source()


def stop_listening() -> None:
    global ears
    if ears is not None:
        ears.stop()
        ears = None
    print("stopped listening")


async def head_loop() -> None:
    """Sends the tracker's head moves to the robot; pauses idle glances while
    a face is being followed and resumes them when it's lost."""
    was_tracking = False
    while True:
        pan, tilt, tracking = await head_moves.get()
        if tracking != was_tracking:
            print("tracking: face found — head follows" if tracking else "tracking: face lost — idle glances resume")
            was_tracking = tracking
        if pan is not None:
            await send_to_robot({"type": "pan", "deg": pan})
        if tilt is not None:
            await send_to_robot({"type": "tilt", "deg": tilt})
        eyes.tracking_info = {"tracking": tracking, "pan": pan, "tilt": tilt}


def staying_up(now: float | None = None) -> bool:
    """Awake on the clock, or while a reply is being composed or spoken.

    `thinking` covers the model call and the gap before the first audio.
    `ears.muted` covers playback, including a canned line such as goodnight.
    """
    if thinking:
        return True
    if ears is not None and ears.muted.is_set():
        return True
    moment = time.time() if now is None else now
    return moment < awake_until


def finish_turn(finished: bool) -> None:
    """End a conversation turn. A finished reply refreshes the awake clock
    before `thinking` clears, so doze cannot fire in that gap."""
    global awake_until, thinking
    if finished:
        awake_until = time.time() + config.AWAKE_SECONDS
    thinking = False


async def doze_loop() -> None:
    """When the awake clock runs out, he nods off on his own (sleepy face,
    no announcement). Saying "hey Rocky" wakes him again. He does not nod
    off while a reply is being composed or spoken."""
    was_awake = False
    while True:
        await asyncio.sleep(1)
        awake = staying_up()
        if was_awake and not awake:
            print(f"({config.ROBOT_NAME} dozed off — say \"hey {config.ROBOT_NAME}\" to wake him)")
            await send_to_robot({"type": "asleep", "on": True})
            await set_head_held(False)  # idle life resumes; the head may wander again
        was_awake = awake


async def _next_heard() -> tuple[str, float, float, float] | None:
    """Wait for the rest of an interrupted question. Keeps waiting while the
    ears still hear speech; gives up after a few quiet seconds."""
    waited = 0.0
    while True:
        try:
            return await asyncio.wait_for(heard.get(), timeout=0.5)
        except asyncio.TimeoutError:
            waited += 0.5
            hearing = ears is not None and ears.hearing
            if (not hearing and waited >= 4.0) or waited >= 30.0:
                return None


async def voice_loop() -> None:
    """Turns what the ears hear into conversations. A bug in one turn must
    not kill the loop (then nothing you say would get through), so each turn
    is guarded."""
    while True:
        item = await heard.get()
        try:
            await _handle_heard(item)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"(voice loop error, ignoring that turn: {e})")


async def _handle_heard(item: tuple[str, float, float, float]) -> None:
    global awake_until, thinking
    text, started_at, ended_at, heard_at = item
    woke, question = strip_wake_word(text)
    # Judge the follow-up window by when you STARTED talking, not by when
    # the transcript arrived — long sentences shouldn't time out.
    if not woke and started_at >= awake_until:
        print(f"(heard, ignoring: {text})")
        return
    print(f"{config.HUMAN_NAME}: {text}")
    eyes.last_heard = text
    awake_until = time.time() + config.AWAKE_SECONDS  # anything you say keeps him up
    norm = normalize(text)
    lines = personality.LINES
    # Face tracking is not decided here. Substring phrases ("look at me",
    # "follow me") used to start tracking before the model saw the sentence,
    # so "don't look at me" turned it on. track_face does that job instead.
    if any(p in norm for p in config.SLEEP_PHRASES):
        # "Rocky, sleep": goodnight line, sleepy face, and only "hey Rocky"
        # wakes him. No brain call. thinking stays set until the line has
        # finished and the asleep face is sent, so doze cannot cut in.
        awake_until = 0.0
        thinking = True
        try:
            print(f"{config.ROBOT_NAME} [sleepy]: {lines['sleep']}")
            await send_to_robot({"type": "emotion", "name": "sleepy"})
            await say(lines["sleep"])
            await send_to_robot({"type": "asleep", "on": True})
        finally:
            thinking = False
        awake_until = 0.0  # say() doesn't touch it, but be explicit
        return
    if woke:
        # Heard his name: eyes open, perk up to eye level (tilt can't go
        # above 0 on this build — the platform would hit the pan servo).
        await send_to_robot({"type": "asleep", "on": False})
        await send_to_robot({"type": "emotion", "name": "surprised"})
        if tracker is None or not tracker.tracking:
            await send_to_robot({"type": "tilt", "deg": 0})
            if tracker is not None:
                tracker.note_pose(tilt=0)
    if woke and not question:
        # Just "hey Rocky" — wait for the actual question.
        await say(lines["wake"])
        awake_until = time.time() + config.AWAKE_SECONDS
        return
    finished = await converse(question, ended_at, heard_at)
    while not finished:
        # He was cut off while thinking: wait for the rest of the sentence
        # and answer the whole thing once.
        item = await _next_heard()
        if item is None:
            print("  (didn't catch the rest — answering what I heard)")
            finished = await converse(question)
            continue
        text, started_at, ended_at, heard_at = item
        _, more = strip_wake_word(text)
        print(f"{config.HUMAN_NAME}: {text}")
        question = f"{question} {more}".strip()
        eyes.last_heard = question
        awake_until = time.time() + config.AWAKE_SECONDS
        finished = await converse(question, ended_at, heard_at)


async def console_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # EOF
            break
        if not await handle_console_line(line):
            break


async def main() -> None:
    print(f"{config.ROBOT_NAME} brain server — model {config.MODEL}")
    print(f"listening for the robot on ws://0.0.0.0:{config.PORT}")
    eyes.state_provider = console_state
    eyes.command_handler = console_command
    eyes.serve(config.LIVE_VIEW_PORT, config.LIVE_VIEW_BIND)
    print(f"live view + controls: http://localhost:{config.LIVE_VIEW_PORT}/  (this computer only)")
    if not os.environ.get("ROBOT_TOKEN"):
        print("WARNING: ROBOT_TOKEN is not set in server/.env — the robot will be refused")
    global tracker, brain, main_loop
    loop = asyncio.get_running_loop()
    main_loop = loop
    brain = RobotBrain(ABILITIES)
    tracker = Tracker(eyes, lambda p, t, on: loop.call_soon_threadsafe(head_moves.put_nowait, (p, t, on)))
    eyes.has_annotator = True
    tracker.start()
    print("face tracking:", "on" if config.TRACKING else f"off — say \"{config.ROBOT_NAME}, track me\" or type `track on`")
    print("type `ask <question>` to talk to the brain right now\n")
    async with websockets.serve(handle_robot, "0.0.0.0", config.PORT, max_size=256 * 1024):
        voice_task = asyncio.create_task(voice_loop())
        doze_task = asyncio.create_task(doze_loop())
        head_task = asyncio.create_task(head_loop())
        pick_camera_source()
        if config.LISTEN_ON_START:
            await start_listening()
        try:
            await console_loop()
        finally:
            voice_task.cancel()
            doze_task.cancel()
            head_task.cancel()
            mac_camera.stop()
            if ears is not None:
                stop_listening()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
