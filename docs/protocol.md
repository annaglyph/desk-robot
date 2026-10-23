# Robot ↔ brain WebSocket protocol (v0)

The robot connects to `ws://<mac-ip>:8765`. The first message must be a
`hello` carrying the shared `ROBOT_TOKEN` (server/.env, firmware secrets.h);
the server drops anything else without replying, and allows one robot at a
time. Plain `ws://` on the home LAN — no TLS in v1. Text frames are JSON. Binary
frames start with one type byte: `0x01` = mic audio, `0x02` = camera JPEG.
The rest of the frame is the payload.

## Robot → server

```json
{"type": "hello", "who": "desk-robot", "fw": "0.3.0", "token": "..."}  // must be the first message; token = ROBOT_TOKEN
{"type": "state", "pan": 12.5, "tilt": 0.0, "idle": false, "emotion": "neutral"}  // every ~5 s. idle is the switch (enabled), not the gate
{"type": "idle_ack", "id": 1, "idle": false, "pan": 12.5, "tilt": 0.0}  // reply to a glance that carried an id; pan/tilt are current angles
{"type": "temp", "c": 52.0}       // chip temperature, sent every ~10 s
{"type": "speak_done"}            // finished playing the last reply
```

Binary `0x01` frames: microphone audio, 16 kHz mono signed 16-bit PCM,
little-endian, 30 ms (480 samples) per frame.

Binary `0x02` frames: one camera JPEG per frame, QVGA (320×240) by
default, sent continuously at `fps` while streaming is on. The server keeps
the latest frame for the live-view page, the face tracker, and the language model.

## Server → robot

```json
{"type": "emotion", "name": "happy"}
{"type": "pan", "deg": -20}
{"type": "tilt", "deg": 15}
{"type": "speak_begin", "bytes": 0}      // binary TTS audio frames follow; bytes = total if known, 0 = streaming (robot starts after 200 ms of audio)
{"type": "speak_end"}             // no more audio; robot replies speak_done when played out
{"type": "volume", "level": 0.4}  // speaker volume 0.0-1.0
{"type": "asleep", "on": true}    // eyes shut + Z's; false = wake up. Asleep = no idle glances.
{"type": "glance", "on": false, "id": 1}  // idle switch. id is optional; with an id the robot replies idle_ack after applying it
{"type": "mic", "on": true}       // stream the microphone to the server
{"type": "stream", "on": true, "fps": 10}   // start/stop the camera stream, set rate
```

The server sends `stream on` at `CAMERA_FPS` when the robot connects. The
live view is served by `server/brain/eyes.py` at http://localhost:8766/.

Binary frames: type byte `0x01` then TTS audio for the speaker, same
PCM format as above, ~100 ms per frame. The server streams each reply as
the voice generates it (`bytes: 0`); the robot starts playing after 200 ms
of audio is buffered and keeps a 32 s ring buffer in PSRAM. The server
waits for `speak_done` before it un-mutes the microphone.

Firmware side: `firmware/src/link.cpp` maps each server message onto the
same text commands the USB console uses (`emo`, `pan`, `tilt`), so both
paths behave identically. The robot sends `state` every 5 s.

Keep this file in sync with `firmware/src/link.cpp` and `server/brain/main.py`
whenever a message type is added.
