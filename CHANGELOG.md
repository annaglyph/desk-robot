# Changelog

Each version of the robot is a git tag and a GitHub Release. `main` is the
latest work. A video matches the tagged version it was made with.

## Unreleased (V2, in progress)

- The Mac webcam fills in when the robot is offline, the same way the
  microphone already did. A named mic or camera that is unplugged is
  skipped. Face tracking does not turn the head for a camera that stays
  on the desk.
- Notes from the local voice, vision, and personality runs are kept in
  the repo. Virtualenvs, downloaded weights, and generated audio stay
  on disk and out of git.
- "Don't look at me" no longer starts face tracking. A substring match
  was treating that as "look at me" before the model saw the sentence.
  Tracking requests go through the tool loop, which understands the
  negation.
- A read-only Home Assistant adapter can check an allow-listed sensor
  and always returns a structured result. It is not one of Rocky's
  abilities yet, so he cannot call it or change a device.
- The web console shows the face he is making, with or without the
  robot connected. The OLED drawing stays in the firmware.
- Household facts load from a private file at startup and stay out of
  git. He is told to say them as things he knows, not as a file, notes,
  or a prompt. The example file is safe to copy in as-is.
- The language-model URL and model name can be set in `server/.env`.
  Left blank, they stay OpenRouter and Claude Haiku 4.5.
- He no longer nods off while he is thinking or speaking. A finished
  reply refreshes the awake clock before that busy state clears, so a
  long answer cannot doze in the gap. The goodnight line still finishes
  before he goes to sleep.
- After he is told to look, he waits a bounded time for a camera frame
  newer than the one from before the move. An older frame is not called
  fresh, and a dead camera cannot hang the conversation. The Mac webcam
  still answers immediately and does not move the head.
- `look` is for an explicit look or a question that actually needs a
  picture, including another look when the current one is not enough.
  Mentioning a person or object is not a reason to look. "Who is Anna?"
  no longer attaches a camera frame. "Who is that?" still does.
- A model round that calls a tool is not spoken aloud, and a later round
  may still call another tool. The three-sentence cap applies only once
  the round has finished and it did not call a tool. Later emotion tags
  such as `[happy]` are not read out.
- New 3D-printed pan/tilt neck: geared 360° pan, unrestricted tilt, flat
  head plate. Replaces the off-the-shelf SG90-style bracket.

## v1.0 - 2026-09-22

The robot in the videos: XIAO ESP32S3 Sense, OLED face, off-the-shelf
pan/tilt bracket with SG90 servos, camera, PDM mic, MAX98357A amp and
speaker; Python brain on your computer with wake word, speech-to-text,
a language model, and a cloned voice.

- Fixed: the voice was crackly on the robot's speaker since the release
  prep on 2026-09-17. Back to the original (2026-09-06) voice pipeline:
  the original loudness and gain cap, and a limiter that turns loud
  chunks down instead of clipping them. The bass-cut and presence
  sliders are still on the console, off by default.
