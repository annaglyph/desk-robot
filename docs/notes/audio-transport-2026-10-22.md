# Rocky audio transport — 22 October 2026

The network path was identified the next day. See
[network-2026-10-23.md](network-2026-10-23.md). These notes stay as the
record of the transport changes. The cause is not identified in this file. Firmware behaviour, frame size, the 200 ms prebuffer, and I²S
were left as they were at the end of the day. The brain changes are the
`TCP_NODELAY` setting and the speech trace.

Current firmware flags, in `firmware/platformio.ini`:

- `CAMERA_ENABLED=0`
- `MIC_ENABLED=0`
- `LOOP_TIMING=1`

`firmware/scripts/patch_ws_read_probe.py` times the WebSocket library's
payload read. That probe is part of this diagnostic build.

## What changed

The Arduino loop was stalling for hundreds of milliseconds inside
`ws_.loop()` while speech arrived. The probe put that time in the payload
wait, with the TCP read and the speaker feed near zero. Servo steps were
sharing a timer with the OLED transfer, and the OLED accounted for the
steady ~31 ms.

Two structural changes followed:

- Servo steps stay on the 33 ms cadence. The OLED refresh is on its own
  50 ms timer (`OLED_INTERVAL_MS` in `firmware/include/config.h`).
- A FreeRTOS task named `link` owns Wi-Fi and the WebSocket client. It runs
  on core 0 at priority 2. The Arduino loop only polls connection state,
  drains a command queue, steps the servos, and refreshes the face. Parsed
  pan, tilt, expression, and similar commands cross in a bounded queue.
  Overflow drops the newest command and counts it. `speak_begin`, audio
  frames, and `speak_end` are handled on the link task so playback order
  matches the socket. See `firmware/src/link.h` and `firmware/src/link.cpp`.

After that split the control loop stayed around the OLED transfer. Command
queue age was a few tens of milliseconds, with no drops. Incoming audio
still waited hundreds of milliseconds for bytes that had not reached the
firmware.

On the brain, the authenticated robot socket sets `TCP_NODELAY`. macOS reads
that option back as 4 when Nagle is off; any non-zero value is treated as
on. The log line is `TCP_NODELAY=on`. Disabling Nagle did not remove the
multi-second `send()` stalls measured before the trace below.

`server/brain/tcp_probe.py` samples the utterance from a side thread, about
every 50 ms, and writes one JSONL file after playback. Each frame records
when it was ready, when `send()` started, and when `send()` returned, in
both `time.monotonic()` and `time.time()`. Samples include the asyncio
write-buffer size and Darwin `TCP_CONNECTION_INFO`: send window, congestion
window, kernel send-buffer occupancy, transmitted bytes, and retransmitted
bytes. Those fields are byte counts. Linux `TCP_INFO` is not read. The files
are local only, under `experiments/out/tcp-send/`, which is gitignored.

PCM frames are still 3,200 bytes (100 ms of 16 kHz s16le). With the type
byte the WebSocket payload is 3,201 bytes, about 3,205 bytes on the wire.
The ESP32-S3 lwIP build used here has MSS 1,436 and a default receive window
of 5,760 bytes, without window scaling. One frame is three TCP segments, and
a full window holds about four.

## Packet capture

One macOS capture of the robot's TCP connection, analysed by hand. The pcap
was not kept in the repo.

- About 45 seconds, 463 TCP packets, about 323 KB of Mac-to-Rocky payload.
- No repeated sequence numbers that would indicate retransmission.
- No zero-window announcements.
- Rocky's advertised receive window moved between about 1,452 and 5,760 bytes.
- Most of the audio sat in a burst between capture seconds 26 and 33.
- Outgoing Mac data packets had gaps, including about one second during the
  transfer.
- While the Mac was sending, Rocky acknowledged data and reopened the window
  promptly.

That capture does not support packet loss or a completely closed receive
window as the main explanation.

## Latest traced utterance

`experiments/out/tcp-send/speech-20261022-183549-201101.jsonl`, which is
gitignored:

- 97 PCM frames, 309,010 PCM bytes.
- `send()` averaged about 0 ms and peaked at about 2 ms.
- The asyncio write buffer stayed at 0 for every sample and every frame.

The same file also shows the kernel send buffer reaching about 85 KB, and
`txretransmitbytes` rising from 2 to 2,146 during the utterance. The window
stayed between about 2,888 and 5,760 bytes. So this utterance left the
asyncio buffer immediately and still was not a clean kernel transfer. That
is an observation from the trace, not an identified cause.

Other traces saved the same evening still show `send()` blocking for one to
three seconds and the asyncio buffer near its 32 KB high-water mark. Speech
is substantially better than the earlier sessions, and some utterances play
cleanly. Glitches remain. The root cause is not identified.

Silence during a glitch comes from the speaker DMA underrunning while the
ring is empty. PCM that arrives late is appended and played afterwards at
16 kHz. It is not discarded, and the pitch does not change.
