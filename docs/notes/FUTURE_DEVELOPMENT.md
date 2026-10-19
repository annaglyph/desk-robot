# Rocky — Future Development

Ideas, improvements, and technical debt worth revisiting as Rocky develops.

This is deliberately **not a roadmap**. Items here should only be implemented when
they solve a real problem or clearly improve Rocky.

The current working system is the baseline. "Another project does it" is not, by
itself, a reason to change anything.

---

## Home Assistant Control

**Status:** ✅ V1 COMPLETE — October 2026

Rocky has constrained Home Assistant read and control capabilities.

V1 provides:

- semantic allow-lists with separate read and control permissions
- live state reads via `get_home_state`
- approved light control via `control_light`
- only explicit `on` / `off` actions
- strict device identity and clarification of partial names
- verified post-action state before success is reported
- fresh reads for current-state questions rather than relying on conversation history
- no raw entity IDs, service names, or generic Home Assistant service calls exposed to the model

Low-consequence approved lights execute immediately when explicitly requested.

Higher-consequence device classes remain intentionally unsupported. If additional
control domains are added later, confirmation policy, action limits, and safety
requirements should be designed for each capability rather than expanding the
generic V1 interface.

---



## Local / Offline Fallback

**Status:** Worth investigating

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
| Haiku 4.5 | Fish Audio | Normal                         |
| Local LLM | Fish Audio | LLM fallback                   |
| Haiku 4.5 | Local TTS  | TTS fallback                   |
| Local LLM | Local TTS  | Fully local / offline fallback |




### Local LLM

Qwen3-VL 4B and 8B have already been tested.

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
being the first local fallback.

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

Haiku 4.5 -> Fish Audio

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

**Status:** Interesting, but do not build yet

Revisit when Rocky's restart amnesia causes a real problem — i.e. he forgets
something we genuinely expected him to remember.

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

This is already the intended behaviour for Home Assistant readings and should apply
to all future physical and software actions.

Examples:

- requested light ON, HA confirms ON -> success
- requested light ON, HA call fails -> failure
- requested head movement, robot unavailable -> failure
- dry-run action -> explicitly say that nothing was changed

The model must never infer success merely because a command was issued.

---



## `look` / Robot Command Failure Feedback

**Status:** Small technical debt — fix next time `look` is touched

Currently a WebSocket send failure can be printed by the server while the model may
still receive a result implying that the head reached the requested pose.

When this area is next modified:

- propagate robot send/command failure into the tool result
- distinguish requested pose from confirmed/known outcome
- never tell the model that physical movement happened when the body did not receive
the command

Do not make a standalone architectural project out of this; fix it naturally when
the relevant code is next touched.

---



## Provider Abstraction

---



## Provider Abstraction

**Status:** No action required

Rocky's current OpenAI-compatible model interface is sufficient for the production
Haiku/OpenRouter setup.

Do not introduce provider classes or a larger abstraction layer unless Rocky
actually needs to support substantially different provider APIs.

The existing environment-configurable base URL and model are preferable to
architecture added for hypothetical future providers.

---



## Events / Observability

**Status:** Keep in mind

Rocky already has useful console output, tool-result logging, live status, and
per-turn timing information.

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

**Status:** Waiting for hardware

Some behaviours cannot be properly accepted using the Mac webcam/body fallback.

Once the XIAO ESP32-S3 Sense and servos are connected, revisit:

- real head movement and settling
- camera freshness after movement
- face tracking behaviour
- body connect/disconnect state
- physical command failure reporting
- servo limits
- audio interaction with the physical body
- actual latency across the complete robot

Mac fallback tests should not be treated as proof of physical movement behaviour.

---



## Body Connected / Disconnected Behaviour

**Status:** Bookmark

Consider whether Rocky should have a small reflex or personality response when his
physical body connects or disconnects.

Do not implement until the physical robot exists and the real connection behaviour
can be observed.

---



## Natural Head Movement & Idle Behaviour

**Status:** Future enhancement — after basic servo control and hardware integration.

Give Rocky more natural, expressive head movements rather than abrupt, mechanical servo positioning.

### Smooth Servo Motion

- Replace constant-speed linear movement with easing curves (e.g. half-cosine/sine ease-in-out) for natural acceleration and deceleration.
- Support configurable movement duration, speed and mechanical limits.
- Allow pan and tilt to move at slightly different speeds for less robotic behaviour.
- Transition smoothly between targets without sudden jumps or stops.

### Autonomous Idle Behaviour

When Rocky is awake but not actively tracking or interacting:

- Occasionally perform small, subtle pan and tilt movements.
- Use randomised intervals, movement amplitudes and pauses to avoid repetitive patterns.
- Include moments of complete stillness — Rocky should appear curious and attentive, not constantly restless.
- Potentially coordinate head movements with OLED eye expressions and glances.

### Movement Priorities

Implement a simple movement controller with clear priorities:

1. **Active tracking / deliberate actions** — highest priority.
2. **Expressive movements** — listening, thinking, speaking.
3. **Autonomous idle movements** — lowest priority.
4. **Sleeping** — no autonomous movement.

Higher-priority actions should interrupt idle movements smoothly, without abrupt servo jumps.

### Design Considerations

- Keep motion control non-blocking so it doesn't interfere with audio, video, networking or facial animation.
- Separate movement requests from low-level servo control.
- Respect calibrated servo limits and avoid unnecessary holding torque, jitter or excessive movement.
- Tune behaviour through physical testing rather than assuming mathematical easing alone will produce natural results.

**Goal:** Rocky should feel subtly alive when awake, with movements that suggest curiosity and attention rather than continuous mechanical activity.

### Motion Personality — Web UI Controls & Time-of-Day Behaviour

**Status:** Future development — not for immediate implementation.

The standalone ESP32 motion laboratory now has three successful idle personality
presets: `calm`, `curious`, and `lively`. All three have been tested on the fully
assembled Rocky and work well.

**Future enhancements:**

- Expose Calm / Curious / Lively presets in the existing brain web UI.
- Add live controls for movement amplitude (`scale`), duration multiplier
  (`duration`), and hold-time range (`pause`).
- Allow adjustments without reflashing firmware.
- Explore automatic time-of-day personalities: lively in the morning, curious
  during the day, calm in the evening.
- Allow manual selection to override automatic scheduling.
- Integrate with the production motion controller without duplicating the
  experimental motion engine.
- Bring the tested idle choreography and the 16-bit PWM resolution into
  Rocky's production firmware. Production motion behaviour stays unchanged
  until that work is done deliberately.

**Reference settings:** Curious with `scale 1.2`, `duration 0.85`, and
`pause 2500 5500` was particularly successful, although it begins to approach
the Lively preset.

**Important:** Preserve the smooth 16-bit PWM output, existing easing curves,
and coordinated pan/tilt movements established during hardware testing.

---



## AI Budget Awareness

**Status:** Future enhancement / partly for amusement

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

Tested Qwen3-VL 4B and 8B.

Result: local vision was useful, but personality, instruction following, latency,
and conversational behaviour were materially worse than Haiku.

**Decision:** Keep Haiku.

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
new behaviour.

### AyhanSh — Mac-AI-Buddy

Architecturally similar Mac-hosted AI desk robot.

Useful ideas identified during review:

- learned semantic memory as a distinct knowledge layer
- explicit action confirmation for consequential operations
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