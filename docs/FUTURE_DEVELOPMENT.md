# Rocky — Future Development

Ideas, improvements, and technical debt worth revisiting as Rocky develops.

This is deliberately **not a roadmap**. Items here should only be implemented when
they solve a real problem or clearly improve Rocky.

The current working system is the baseline. "Another project does it" is not, by
itself, a reason to change anything.

This copy describes **Rocky V2** (`desk-robot-v2`, branch `v2-dev`) as of
27 October 2026. It began as V1's `docs/notes/FUTURE_DEVELOPMENT.md`. Notes that
only apply to V1's firmware are kept and marked as V1, because the findings are
still worth having.

A link in square brackets is a document in this V2 repository. A path that
starts with `desk-robot/` is a historical V1 file. Those files were not copied
here.

---

## Checkpoint — 27 October 2026

Home Assistant is migrated to V2. Done and pushed: commit `7407574` on
`origin/v2-dev`.

- Configured in the dashboard (Settings > Home Assistant): address, token,
  test connection.
- Separate approved lists for readings and for lights. A reading never grants
  control.
- Entity discovery with **Add as reading** / **Add as light**. Nothing is
  allowed until it is added.
- Live sensor reads and physical lamp control on Rocky.
- The lead-in the model writes before a Home Assistant tool call is no longer
  spoken (see Home Assistant Control).
- Full V2 server suite: 242 tests passed.

Details, deviations and the live record are in
[HOME_ASSISTANT_V2_PLAN.md](HOME_ASSISTANT_V2_PLAN.md). Wi-Fi provisioning
(Stage 3B.1) closed on the same day; see
[ROCKY_V2_HANDOVER.md](ROCKY_V2_HANDOVER.md).

---

## Checkpoint — 23 October 2026 (V1 robot)

Historical record from the V1 robot and firmware. It is not a V2 document, and
its notes were not copied into this repository. They remain only in the V1 tree:

- `desk-robot/docs/notes/network-2026-10-23.md` — the 23 October network result
- `desk-robot/docs/notes/audio-transport-2026-10-22.md` — the 22 October transport notes

Microphone streaming works when Rocky joins the Mac's shared Wi-Fi instead of
the house extender. The result and the home setup are in that first V1 note.
The 22 October transport notes remain in the second.

The link task, the microphone queue, the writability check before each uplink
frame, and `TCP_NODELAY` stay. They are part of the working audio path. Do not
change frame size, the prebuffer, or I²S to chase the old glitches: those were
observed on the extender path.

`LOOP_TIMING=0` and `CAMERA_ENABLED=0`. `MIC_ENABLED=1`. The brain speech
trace runs only when `DESK_ROBOT_TCP_TRACE=1`.

### Still open, not next (V1)

- The camera is still compiled out.
- Production servos still use the ESP32Servo pulse path. The motion
  laboratory's 16-bit PWM stays on the spare board.
- The tilt signal is still GPIO43, which is also UART0 TX. The boot-time
  backward kick is recorded and the move to GPIO44 is deferred.

### Next (V1, at the time)

Stage 2 idle ownership is in: the brain switches the laboratory choreography
on when Rocky is quiet, and off for speech, looks, and face tracking. The
60-second awake clock is unchanged. The heartbeat `idle` flag can still
disagree with the brain's ownership record; that follow-up is under
Awake / Idle / Sleep Lifecycle and belongs with the sleep work. Half-cosine
easing on commanded moves is still parked.

### Where V2 stands on the same items

- **Camera:** on in V2. It starts at boot, streams to the brain while
  connected, and was checked on the robot after Wi-Fi Stage 3A. The V1
  compile flags (`LOOP_TIMING`, `CAMERA_ENABLED`, `MIC_ENABLED`) do not exist
  in V2's firmware.
- **Tilt pin:** solved differently. V2 drives tilt from D8 (GPIO7); GPIO43 and
  GPIO44 are unused (`firmware/include/config.h`). D8 is shared with the Sense
  board's microSD slot, which must stay empty. The GPIO44 move is moot.
- **Servo output:** V2 still uses ESP32Servo (MCPWM on the S3). There is no
  16-bit motion-laboratory PWM in V2.
- **Idle ownership and the heartbeat comparison:** V1 only. V2 has no
  laboratory choreography and no `idle_sent`; see Natural Head Movement.

---

## Home Assistant Control

**Status:** ✅ V2 COMPLETE — 27 October 2026 (commit `7407574`). V1 complete
before that.

Rocky has constrained Home Assistant read and control capabilities. V2 keeps
V1's rules and tool descriptions word for word (tests check them against V1
when V1 sits beside V2).

Both versions provide:

- semantic allow-lists with separate read and control permissions
- live state reads via `get_home_state`
- approved light control via `control_light`
- only explicit `on` / `off` actions
- strict device identity and clarification of partial names
- verified post-action state before success is reported
- fresh reads for current-state questions rather than relying on conversation history
- no raw entity IDs, service names, or generic Home Assistant service calls exposed to the model

Low-consequence approved lights execute immediately when explicitly requested.

### What V2 adds

- **Dashboard configuration.** Settings > Home Assistant: an on/off switch,
  the address, the token, Test connection, entity search, Add as reading /
  Add as light, and Edit as text. The readings and lights are text lines in
  `server/settings.json` (git-ignored), checked by V1's parsers: at most 32
  readings and 8 lights, and only `light` or `switch` entities can be
  switched. Changes, removals and switching it off apply at once.
- **Token handling.** The token lives in its own git-ignored, owner-only file
  (`server/home_assistant.token`), not in `settings.json` or `.env`. The
  dashboard writes it and can clear it. No page or API response ever returns
  it, and it stays out of logs and the model. V2 does not read V1's
  `HOME_ASSISTANT_*` / `HA_*` keys from `.env`; moving from V1 is a manual
  copy into the card.
- **Prompt rules only while offered.** V1's Home Assistant prompt rules are
  added to the character's prompt only while a home tool is offered.
- **Speech.** V2 speaks each sentence as soon as it is complete. While a home
  tool is offered, a model round is held until it is known whether it calls
  `get_home_state` or `control_light`. If it does, that round's lead-in stays
  in the transcript and is not spoken, as in V1; the answer after the result is
  spoken. Ordinary replies and quick actions still speak; when Home Assistant
  is off, sentences are spoken as they complete.

The trade-off: with home tools offered, his first word waits until that model
round finishes (a 250 ms local probe measured about 260 ms). There is no extra
model call.

### Not part of the completed integration

Higher-consequence device classes remain intentionally unsupported. If additional
control domains are added later, confirmation policy, action limits, and safety
requirements should be designed for each capability rather than expanding the
generic V1 interface.

---



## Local / Offline Fallback

**Status:** Worth investigating. A minimal voice fallback already exists;
there is no brain fallback.

What V2 already has:

- **Speech-to-text is already local.** faster-whisper (`STT_MODEL`
  `base.en`) runs on the Mac, so listening does not depend on a cloud
  service.
- **Per-request voice fallback.** `mouth.stream()` uses Fish Audio when
  `FISH_AUDIO_API_KEY` is set and falls back to the computer's built-in voice
  (macOS `say` with `TTS_FALLBACK_VOICE`, `Fred` by default) when Fish is not
  set up or the request fails. It decides again for every sentence: there is no
  provider state, no announcement, and no distinction between a short timeout,
  a bad key and exhausted credit.
- **No brain fallback.** If the model provider cannot be reached, he says a
  fixed in-character error line ("Brain cannot reach internet…", "Brain has no
  key…", or the API status code) and the turn ends.

Local models are not currently preferred as Rocky's primary brain or voice, but
they may be valuable as automatic fallbacks when cloud services are unavailable.

The objective is graceful degradation:

- OpenRouter credit exhausted
- OpenRouter/API unavailable
- Fish Audio credit exhausted
- Fish Audio/API unavailable
- internet connection unavailable

Brain and voice fallback should be independent.

### Preferred operating modes


| LLM       | TTS        | Mode                           |
| --------- | ---------- | ------------------------------ |
| Haiku 5.5 | Fish Audio | Normal                         |
| Local LLM | Fish Audio | LLM fallback                   |
| Haiku 5.5 | Local TTS  | TTS fallback                   |
| Local LLM | Local TTS  | Fully local / offline fallback |

V2's default model is `anthropic/claude-haiku-5.5` through OpenRouter (V1 and
the earlier tests used Haiku 4.5). Today "Local TTS" means only the built-in
`say` voice.




### Local LLM

Qwen3-VL 4B and 8B have already been tested (against Haiku 4.5, on V1).

Neither matched Haiku for Rocky's personality, instruction following, or tool
behaviour, so they are not preferred production brains.

However, Qwen3-VL 4B:

- accepted Rocky's existing multimodal image format
- had useful local vision
- was fast enough after warm-up
- used substantially less memory than the 8B model

This may make the 4B model a good degraded-mode fallback.

Revisit it specifically as a fallback rather than trying to make it beat Haiku.

The fallback must still respect Rocky's normal safety/tool boundaries. Local mode
must not gain abilities that production mode does not have.

### Local TTS

Fish Audio remains Rocky's preferred voice.

A fallback voice does **not** need to sound identical to Fish Rocky. It should be:

- natural enough for normal conversation
- fast to first audio
- reliable on Apple Silicon
- fully local
- appropriately licensed
- streamable if practical

Chatterbox Turbo MLX was previously tested and was technically viable, with good
warm performance, but its default voice was not suitable as Rocky's primary voice.

It may be reconsidered as a fallback, alongside other lightweight local TTS
options.

macOS `say` should remain the final emergency fallback rather than necessarily
being the first local fallback. In V2 it is currently the only one.

### Failover behaviour

Do not switch providers merely because one request times out briefly.

Fallback should distinguish between:

- transient request failure
- authentication failure
- quota / credit exhaustion
- provider outage
- network unavailable

Avoid repeatedly retrying a known-unavailable cloud provider on every turn.

When entering a degraded mode, Rocky should acknowledge it once in character rather
than silently changing behaviour or voice.

Example concept:

> Cloud brain not answering. Using smaller brain now. Still Rocky. Slightly less amaze.

When normal service becomes available again, recovery should also be clean and
should not interrupt an active conversation.

### Architecture

Do not rewrite the current production path around fallback support.

Prefer a thin failover layer around the existing LLM and TTS boundaries.

The production path remains:

Haiku 5.5 -> Fish Audio

Fallback capability should be additive and independently removable.

### Acceptance criteria

A fallback implementation is useful only if Rocky can continue a basic conversation
when the internet or either paid service is unavailable.

Test at minimum:

- OpenRouter unavailable, Fish available
- Fish unavailable, OpenRouter available
- both unavailable
- internet disconnected
- recovery when service returns
- camera/vision in local LLM mode
- normal read-only tools in local mode
- tool failure honesty
- no repeated fallback announcements

The local fallback does not have to equal normal Rocky.

It has to remain recognisably Rocky and remain useful.

---



## Learned / Semantic Memory

**Status:** Interesting, but do not build yet. Personal context is done; learned
memory is not.

Revisit when Rocky's restart amnesia causes a real problem — i.e. he forgets
something we genuinely expected him to remember.

Layer 2 exists in V2 (commit `9036384`): `personality.py` loads
`server/personal_context.txt` (git-ignored; `personal_context.example.txt` is
the template) into the prompt of whichever character is active. Conversation
history is in memory only and is lost on restart. There is no learned-memory
store.

Potential architecture:

1. **Personality** — who Rocky is
2. **Personal context** — stable, curated facts Rocky starts knowing
3. **Learned memory** — useful things learned during previous conversations
4. **Conversation history** — recent interaction
5. **Tools** — current/live information and actions

`personal_context.txt` should remain separate from learned memory. Stable curated
facts should not depend on semantic retrieval.

### Possible implementation

If implemented:

- local embeddings
- small local SQLite store
- retrieve only a small number of relevant memories
- label retrieved content clearly as prior knowledge/memory
- memory failure must not prevent normal conversation
- provide a way for the human to inspect and delete memories
- support updating/correcting facts rather than silently preserving stale wording
- handle conflicting memories explicitly
- keep private memory local by default

Consider explicit `remember`, `recall`, and `forget` abilities, but decide separately
whether useful memories should also be captured automatically.

Do not add a vector database simply because an AI robot is expected to have one.

---



## Tool Outcome Honesty

**Status:** Principle to apply to all new tools

The result returned to the model should describe what **actually happened**, not
what Rocky attempted to do.

In V2 this holds for Home Assistant reads and light control: a failure reaches
the model as a failure, and a light is on or off only after Home Assistant shows
that state. Computer control returns refusals and errors as results. `dance`
says when no body is connected. The known gap is `look` (next section). Apply
the principle to all future physical and software actions.

Examples:

- requested light ON, HA confirms ON -> success
- requested light ON, HA call fails -> failure
- requested head movement, robot unavailable -> failure
- dry-run action -> explicitly say that nothing was changed

The model must never infer success merely because a command was issued.

---



## `look` / Robot Command Failure Feedback

**Status:** Small technical debt — fix next time `look` is touched

Still true in V2. `send_to_robot` prints "(no robot connected — command not
sent)" and returns `False`, but `look` ignores that. It records the pose and
returns "Head is now at pan … tilt …", which says the move happened when it did
not. With `see` on, the camera part is honest ("No camera image available.") and
the pose claim is not.

When this area is next modified:

- propagate robot send/command failure into the tool result
- distinguish requested pose from confirmed/known outcome
- never tell the model that physical movement happened when the body did not receive
the command

Do not make a standalone architectural project out of this; fix it naturally when
the relevant code is next touched.

---



## Provider Abstraction

**Status:** No action required

Rocky's current OpenAI-compatible model interface is sufficient for the production
Haiku/OpenRouter setup. In V2, `LLM_BASE_URL` and `LLM_MODEL` in `server/.env`
choose the provider and model (OpenRouter by default; Anthropic's
OpenAI-compatible endpoint also works). The only per-provider code is the
request option that keeps Haiku 5.5's hidden reasoning off.

Do not introduce provider classes or a larger abstraction layer unless Rocky
actually needs to support substantially different provider APIs.

The existing environment-configurable base URL and model are preferable to
architecture added for hypothetical future providers.

---



## Events / Observability

**Status:** Keep in mind

Rocky already has useful console output, tool-result logging, live status, and
per-turn timing information. In V2 each turn logs a timing line (heard, picture,
first sentence, first audio, done). The dashboard polls `/status`, which includes
the brain's `console_state`: awake time, speech, computer and Home Assistant
status. The launcher shows the server log. There is no event bus.

Do not add an event bus simply for architectural neatness.

Revisit if multiple independent systems eventually need to subscribe to the same
internal events, for example:

- web UI
- logging/diagnostics
- memory
- automation
- hardware telemetry

Until then, keep the implementation simple.

---



## Physical Robot Integration

**Status:** Partially complete. The V2 body is connected and in daily use. A few
items remain.

Some behaviours cannot be properly accepted using the Mac webcam/body fallback.

On the V2 robot (XIAO ESP32-S3 Sense, printed pan/tilt neck, MG90S servos):

- **Real head movement and settling:** done. `look`, face tracking, body
  language, dances and the roaming idle screen run on the hardware.
- **Camera freshness after movement:** done. `look` waits for the tracker's
  estimate of the move's arrival plus `LOOK_SETTLE_S` (0.3 s), then takes a
  sharp still from the new angle.
- **Face tracking behaviour:** done, on the robot camera.
- **Servo limits:** calibrated to the tested neck in commit `76844ff`. The
  firmware limits, the Python tracker, the dashboard sliders and
  `tests/test_motion_config.py` share them. The operational reference is
  `firmware/include/config.h`: `PAN_GEAR_RATIO` 1.0 and head limits of ±70°.
  `CHANGELOG.md` (v2.0) still says the printed neck gears pan up 38:30, about
  ±95°. That sentence has not been reconciled with the measured stops in the
  config comments. It is an unresolved documentation observation; the firmware
  was not changed to match the changelog.
- **Audio with the physical body:** in use. The camera, microphone, OLED,
  servos and conversation all worked during the Wi-Fi Stage 3A hardware run.
- **Body connect/disconnect state:** still a bookmark (below).
- **Physical command failure reporting:** still open (`look` section above).
- **Actual latency across the complete robot:** each turn logs its timing, but
  no end-to-end figure is recorded here.

Mac fallback tests should not be treated as proof of physical movement behaviour.

---



## Body Connected / Disconnected Behaviour

**Status:** Bookmark

Consider whether Rocky should have a small reflex or personality response when his
physical body connects or disconnects.

Do not implement until the physical robot exists and the real connection behaviour
can be observed.

The robot exists now, so this can be observed, but nothing has been decided. In V2
today: on connect the brain logs `robot connected!` and sends the robot its stream,
glance, settings, face pack, volume and asleep state. On disconnect the brain logs
`robot disconnected` and, on the default `auto` microphone setting, switches to
the Mac's microphone. The robot stops its mic
and camera stream, and an awake face goes to sleep. Neither side says anything.

---



## Natural Head Movement & Idle Behaviour

**Status:** Smooth motion, expressive movement while talking, and the movement
priorities are in V2. **Awake idle personality is deferred:** a deliberate
decision (27 October 2026) not to develop more awake idle behaviour for now.
This includes Calm / Curious / Lively. It is not an active priority.

Give Rocky more natural, expressive head movements rather than abrupt, mechanical servo positioning.

### Smooth Servo Motion

**V2: complete, using a different method.** `ServoNeck::update` uses an
acceleration-limited speed profile: it speeds up, cruises, and slows to stop
exactly on the target, with no overshoot. If the target changes mid-move, it
bends toward the new one without a jump. Pan and tilt each have their own speed
and acceleration (`PAN_MAX_SPEED` / `PAN_ACCEL`, `TILT_MAX_SPEED` /
`TILT_ACCEL`) and calibrated limits. The roaming idle screen slows the quicker
axis so both arrive together. Half-cosine easing was never needed for commanded
moves and stays parked.

The original list:

- Replace constant-speed linear movement with easing curves (e.g. half-cosine/sine ease-in-out) for natural acceleration and deceleration.
- Support configurable movement duration, speed and mechanical limits.
- Allow pan and tilt to move at slightly different speeds for less robotic behaviour.
- Transition smoothly between targets without sudden jumps or stops.

### Autonomous Idle Behaviour

**V2: deferred while awake. A version exists while asleep.**

- **Awake:** no autonomous head movement. The brain sends `glance off` on
  connect and leaves it off, because idle glances fought deliberate looks. The
  firmware's simple random glance in `ServoNeck` is still there, unused. The
  eyes still blink and glance on the OLED.
- **Asleep, "Look around" idle screen:** Casey's roaming behaviour. With the
  neutral face up (the clock at night), the head looks round the room every few
  seconds, the eyes leading and both axes arriving together. Looks near level
  are held longer so the tilt servo can rest. The brain is told when the head
  moves, so a turning camera does not count as a wave.

When Rocky is awake but not actively tracking or interacting, the original idea
was to:

- Occasionally perform small, subtle pan and tilt movements.
- Use randomised intervals, movement amplitudes and pauses to avoid repetitive patterns.
- Include moments of complete stillness — Rocky should appear curious and attentive, not constantly restless.
- Potentially coordinate head movements with OLED eye expressions and glances.

### Movement Priorities

**V2: in place in the brain, without a separate controller.** `body.py` (body
language) moves only while he is awake, not asleep or sleepy, and not while face
tracking is on. `look` holds the body language for the rest of that answer.
Turning tracking on stops the body language: the tracker owns the head. Moving
the head from the dashboard, a dance, or falling asleep stops it too. There are
no awake idle movements to interrupt.

1. **Active tracking / deliberate actions** — highest priority.
2. **Expressive movements** — listening, thinking, speaking.
3. **Autonomous idle movements** — lowest priority.
4. **Sleeping** — no autonomous movement.

Higher-priority actions should interrupt idle movements smoothly, without abrupt servo jumps.

What V2 does for "expressive": a short emotion gesture when his voice starts,
then that emotion's posture with a small nod or drift every few seconds, and a
glance up while he works out a reply. Pauses are long enough for the servos to go
quiet. It can be switched off with *Body language* in the dashboard.

### Design Considerations

- Keep motion control non-blocking so it doesn't interfere with audio, video, networking or facial animation.
- Separate movement requests from low-level servo control.
- Respect calibrated servo limits and avoid unnecessary holding torque, jitter or excessive movement.
- Tune behaviour through physical testing rather than assuming mathematical easing alone will produce natural results.

**Goal:** Rocky should feel subtly alive when awake, with movements that suggest curiosity and attention rather than continuous mechanical activity.

### Motion Personality — Web UI Controls & Time-of-Day Behaviour

**Status:** Deferred in V2. Calm / Curious / Lively was built and tuned on V1
(the motion laboratory and its live-view controls). V2 has none of it: no
choreography, no presets, no controls. The handover listed it as a possible
goal, but it is not being pursued and must not become an active priority. The
notes below are V1's, kept for whenever it is reconsidered.

On V1, the live view's Idle personality section selects Calm, Curious, or Lively and
can adjust amplitude, duration, and the pause range. The firmware still
generates every gesture. Choosing a personality does not turn idle on or off.

**Ideas, if it is ever picked up again:**

- Explore automatic time-of-day personalities: lively in the morning, curious
  during the day, calm in the evening.
- Allow manual selection to override automatic scheduling.

**Reference settings:** Curious with `scale 1.2`, `duration 0.85`, and
`pause 2500 5500` was particularly successful, although it begins to approach
the Lively preset.

**Important (V1):** Preserve the smooth 16-bit PWM output, existing easing curves,
and coordinated pan/tilt movements established during hardware testing. If this
ever comes to V2, it should build on V2's `ServoNeck` motion, which uses
ESP32Servo, not V1's laboratory timer.

### Awake / Idle / Sleep Lifecycle

**Status:** V2 has a working lifecycle (below). The rest is deferred: it was
designed around awake idle choreography, which V2 does not have and is not
pursuing.

Rocky should only do expressive idle movement while awake. After a stretch
without meaningful interaction he should return to neutral, stop, and use
the sleep behaviour that already exists.

#### What exists today (V2)

Inactivity is measured only in the Python brain. `AWAKE_SECONDS` defaults to 60
and can be set from 10 to 600 in the dashboard's advanced settings. `awake_until`
is set when he wakes and with each thing said to him, and dances and dashboard
actions extend it. `doze_loop` checks once a second and calls `fall_asleep()`
when the clock runs out. A sleep phrase or the console sleep control also put him
to sleep. A wave seen by the camera wakes him, as does the wake phrase. The
firmware has no inactivity timer.

`fall_asleep()` stops any dance and the body language, turns face tracking off,
sends pan 0 and tilt 0, then sends `asleep`. On `sleep on` the firmware also sets
both axes to 0, shows the sleep face for a few seconds, then the idle screen
(clock, a game, or Look around). The `asleep` message is sent right after the
centring command, not after the move finishes. Once the head is still at level,
both servos go quiet after the relax delay (below).

In V2 a lost brain connection does put an awake face to sleep, and the robot stops
its mic and camera stream. That differs from V1.

#### Follow-up: heartbeat `idle` vs brain ownership (V1 only)

**Obsolete for V2.** V2 has no laboratory choreography, no `idle_sent` and no
`_note_heartbeat` comparison, and idle glances stay off. Kept because it records
how V1's two records could disagree.

Recorded with Stage 2. The comparison below is unchanged on purpose. The
60-second awake clock stays as it is and belongs to this lifecycle work.

The 5-second `state` heartbeat reports `idleMotion.enabled()`, the idle
switch. It does not report whether the gate is open (brain connected and
face awake) or whether a gesture is in progress.

The brain keeps its own `idle_sent`. `_note_heartbeat` treats `idle: true`
while `idle_sent` is false as a serial-console `idle on`. It clears
`idle_sent` to unknown and marks the pose unsynced, so the next move claims
the head again.

Those two records can disagree:

1. A brain process still inside an earlier `awake_until` window sends
   `glance on` after the quiet period, before anyone speaks in this sitting.
   A later glance-off that the brain counts as done (`idle_sent` false),
   while the firmware switch is still on, makes the next heartbeat look like
   a console override.
2. `_sync_head` records idle as off after it sends `glance off`, including
   when the `idle_ack` never arrives. If that off did not reach the firmware,
   or a `glance on` landed afterwards, the heartbeat still shows the switch on.

Hardware face-tracking after the once-per-send correction did not show this
path driving the head. Leave the heartbeat comparison and `AWAKE_SECONDS`
alone until the sleep lifecycle work. A later alignment can report
`enabled && gate` in `state`, or treat a heartbeat `idle: true` as
information and keep pose sync on `idle_ack`.

#### What existed on V1 (recorded with Stage 2)

Kept as V1's record. Several points differ in V2 (see "What exists today (V2)"
above): V2's sleep centres the head, and a disconnect does put the face to sleep.

Inactivity is measured only in the Python brain. `AWAKE_SECONDS` is 60.
`awake_until` is set when a heard utterance is accepted, when a finished
reply ends, when the console turns sleep off, and when the wake phrase is
heard. `doze_loop` checks about once a second. He stays awake while a reply
is being composed (`thinking`) or spoken (`ears.muted`), even if the clock
has run out. The firmware has no inactivity timer.

The clock resets on meaningful speech. It does not reset for camera frames,
face tracking, OLED animation, or the firmware's own glance timer. Speech
that starts after the clock has expired is ignored unless it contains the
wake phrase.

Sleep is entered three ways: the awake clock expires, a sleep phrase
(`goodnight`, `go to sleep`, and the phrases in `SLEEP_PHRASES`), or the
console sleep control. The brain then sends `asleep` on. A sleep phrase
also plays the goodnight line first. The firmware `sleep` command only
changes the face: eyes shut, slow breathing, Z's. It does not move the
head and it does not stop the microphone, the camera stream, or the
WebSocket. Those keep running for as long as the brain is connected.
Disconnecting the brain does not put the face to sleep. It does turn the
USB demo emotion cycle back on, and idle head glances stay off because
they require a connected brain.

Wake is the wake phrase, which opens the eyes, shows surprise, and nods
tilt to 0° when face tracking is not active. The console sleep-off control
opens the eyes and starts another 60-second window. Neither path centres
pan.

On connect the brain claims the head with `glance off` and reads the held
pose from `idle_ack`. After the quiet period it may send `glance on`. A
deliberate look holds its pose, then idle can return. Speech, thinking,
playback, and face tracking (including a still face, while tracking stays
enabled) turn idle off. The firmware plays the Calm/Curious/Lively
choreography only while the switch is on, the brain is connected, and the
face is awake. The heartbeat mismatch above is the open part of this.

#### Servo relaxation and PWM (V2)

Servo relaxation is separate from sleep. V2's `ServoNeck::relax` does not rely
on the library's detach. Between two pulses it takes the pin off the MCPWM output
and drives it low, so no more pulses go out. The source says why: on the S3, the
library's `detach()` leaves MCPWM driving the pin. Re-attaching routes the same
MCPWM output back to the pin between pulses, so the two servos cannot end up on
each other's operator. Servo signal lines are also held low from before
`setup()`, so a floating line cannot jerk a servo at power-up.

- Pan relaxes after `SERVO_RELAX_MS` (1.5 s) still. Tilt relaxes after
  `TILT_RELAX_MS` (1.5 s), but only between `TILT_RELAX_MIN_DEG` (−15°) and
  `TILT_RELAX_MAX_DEG` (+1°). The printed head balances about 4° above level:
  level or a little down, the gears hold it unpowered; tipped up, it falls back.
  Outside that band the tilt keeps holding.
- While one axis moves, the other is held so it cannot sag.
- The periodic re-drive while relaxed (`TILT_REFRESH_MS`) is off because the
  servo hummed each time. If the head ever tips up on its own again, the
  recorded fallbacks are a 10k pull-down on the tilt signal, or
  `TILT_REFRESH_MS` 30000.
- Relaxing stops the signal only. No firmware pin switches off the servos' 5 V
  supply.

Hardware-tested: the relax band and the hum. The firmware comments record
both from tests on this head. Not measured: the pin level on a scope, and
current draw while relaxed.

#### Servo relaxation (V1 record)

The V1 notes from the same stage, kept for comparison:

Servo relaxation is separate from sleep. `ServoNeck::update` eases toward
the current target. Once an axis is within 0.25° and stays there for
`SERVO_RELAX_MS` (1.5 s), it calls `servo_.detach()`. While one axis is
still moving, the other is held so it cannot sag. Detach is a signal
change only. No firmware pin switches off the servos' 5 V supply, so
detach does not remove power. On the ESP32-S3, ESP32Servo 3.2.1's MCPWM
`detachPin` releases the library channel and does not drive the pin low.
Whether the last pulse actually stops has not been measured on this head.
The config comment that the head is light enough to hold is an assumption
about the reference mechanism. On the calibrator, tilt held with PWM off
at level and looking down. Pan was not tested that way, and this printed
head is heavier.

#### Proposed behaviour (deferred)

Written for V1's choreography. In V2 the return to neutral and relaxing after the
sleep centring already happen. A longer awake time can already be set (up to 600
s). The parts that depend on awake idle gestures are deferred with Calm / Curious /
Lively. V2 already judges whether speech counts by when it started, so a long
utterance does not race the clock. Still unbuilt and still worth remembering:
waiting until the centring move has finished before sending `asleep`.

The original proposal:

**Interaction → Awake → Expressive Idle → Return to Neutral → Sleep.**

While awake, use the motion laboratory's Calm, Curious, or Lively
choreography. Idle gestures must not refresh the awake clock. Face
tracking must not either. Accepted speech, an explicit command, and
intentional engagement do refresh it. Speaking and listening already keep
him up through `thinking` and `ears.muted`; a speech onset should also
count, so a long utterance is not racing a short timer. The timeout
becomes a brain setting, default five minutes, replacing the 60-second
doze for this lifecycle.

When the timer expires:

1. Stop scheduling idle gestures.
2. Command pan and tilt to calibrated neutral (0°).
3. Wait until that move has finished.
4. Send the existing `asleep` message.
5. Let the existing rest timer detach the signal only after both axes
   have been still. Do not detach on the sleep message itself, and do not
   detach before the return finishes.

A wake phrase or other intentional interaction during the return or during
sleep cancels the sequence, opens the eyes, and starts a new awake window.
Explicit `look`, speech, and tracking outrank idle. One motion owner only:
extend `ServoNeck`, including the laboratory's 16-bit timer and easing,
rather than running a second scheduler beside it. The brain decides when
he is idle, centred, or asleep. The firmware executes one target at a
time.

The 1.5-second relax must be suspended while an idle pose is being held.
Otherwise a 2–6 second Curious hold would drop the signal halfway through
and the next gesture would reattach from wherever the head had sagged.
Relax after the sleep centre, not during the performance.

If the brain disconnects mid-return, the firmware should finish the
neutral target it already has, then relax. It should not invent a new
idle pattern without the brain.

---



## AI Budget Awareness

**Status:** Future enhancement / partly for amusement. Nothing in V2 reads a
balance yet.

Investigate whether Rocky can read the remaining credit or usage allowance for the
cloud services that keep him running.

Potential sources:

- OpenRouter credit / remaining balance
- Fish Audio credit / remaining balance, if a suitable API is available

This should be implemented as read-only system information. API credentials and
account details must remain inside the server-side adapter and must never be exposed
to the model.

### Practical use

Budget information could support the local/offline fallback system.

For example:

- warn when cloud credit is becoming low
- display remaining budget in the web UI
- prepare or activate a local fallback before a provider rejects requests
- distinguish quota/credit exhaustion from ordinary API or network failures

Fallback thresholds and provider selection must be controlled by server logic,
**not by the LLM**.

Rocky may know about his remaining budget, but he does not get to decide whether
to disable himself, conserve requests, purchase credit, or change providers.

### Personality

If budget information is exposed as a Rocky-readable status, allow him to react to
it naturally and in character.

Potential behaviour:

> Question, Rus. Why you spend thirty pounds on filament but only give me ten
> dollars brain money?

At low Fish Audio balance:

> Bad bad bad. Voice budget nearly gone. Soon I sound like Fred.

(Fred is literally true: `Fred` is V2's default built-in fallback voice.)

At critically low OpenRouter balance:

> I have forty-two cents of brain left. This is concerning.

This is intentionally allowed to be funny.

However, the underlying budget/failover mechanism must remain deterministic and
outside the model. Rocky can be existentially worried about his financial demise;
he cannot control it.

### Investigation

Before implementation:

- confirm supported balance/usage APIs for OpenRouter and Fish Audio
- determine whether they require the normal API keys or separate account credentials
- ensure returned information contains no sensitive account data
- establish sensible polling/caching rather than checking on every conversation
- decide whether budget is available as a model tool, web-UI status, or both
- integrate with Local / Offline Fallback only after the fallback mechanism exists

---



## Ideas Reviewed and Currently Rejected

These are recorded to avoid repeatedly reconsidering them without a new reason.

### Replace Haiku with local Qwen

Tested Qwen3-VL 4B and 8B (against Haiku 4.5, on V1).

Result: local vision was useful, but personality, instruction following, latency,
and conversational behaviour were materially worse than Haiku.

**Decision:** Keep Haiku. V2 uses Haiku 5.5. The comparison was not re-run
against it, and there is no new reason to.

### Replace Fish with local Chatterbox

Engineering performance was acceptable after warm-up, but the voice did not suit
Rocky.

**Decision:** Keep Fish.

### Generic event bus

No current requirement justifies the additional architecture.

**Decision:** Revisit only if multiple independent event consumers emerge.

### Twitter / X / social posting

Rocky does not need autonomous social-media abilities.

**Decision:** Out of scope.

---



## External Projects Worth Watching



### Casey Grothaus — Desk Robot

Rocky's original upstream project.

Continue reviewing upstream changes selectively rather than automatically merging
new behaviour. V2 work stays on `v2-dev`; local `main` tracks Casey's upstream
and is not merged, rebased or pushed without an explicit decision. V2's "Look
around" roaming idle screen is Casey's.

### AyhanSh — Mac-AI-Buddy

Architecturally similar Mac-hosted AI desk robot.

Useful ideas identified during review:

- learned semantic memory as a distinct knowledge layer (not built; see Learned / Semantic Memory)
- explicit action confirmation for consequential operations (V2 asks before running an Apple Shortcut; Home Assistant keeps higher-consequence devices out entirely)
- treating model output as untrusted
- tool results describing actual outcomes
- clearly labelling retrieved/outside information as knowledge

Its architecture should be treated as inspiration only.

At the time of review the repository had no `LICENSE` file, so source code should
not be copied into Rocky.

---



## Rule for Adding Future Work

Before implementing an item from this file, ask:

> What problem does this solve for Rocky today?

If the answer is only:

> "It would be cool."

...that may still be a perfectly valid reason — Rocky is a desk robot, after all —
but make that decision consciously rather than disguising it as an engineering
requirement.