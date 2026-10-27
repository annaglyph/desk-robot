# Home Assistant in V2 — investigation and plan

Status: implemented on `v2-dev` (uncommitted). Offline tests passing (234). Live acceptance started 27 Oct 2026 and is blocked on macOS Local Network privacy; no entity was added and no light was switched. See sections 11 and 12. Sections 1–10 are the approved plan, kept as written. No V1 file was changed. No credential was read, printed, or copied.

V2 baseline: `v2-dev` at `807a62b` (Stage 3B.1 Wi-Fi, pushed). Working tree has only the intentional Stage 1 proof (`firmware/platformio.ini`, `firmware/test/setup_ap/`).

V1 reference: `/Users/rus/Development/Python/desk-robot`, branch `main`. The Home Assistant files below are committed there (`12513d5`, `6dddf6c`, `7d7ede0`, `635a66a`). V1’s working tree also has untracked `experiments/home-assistant-tools/` and unrelated edits; this plan uses only the committed files.

## 1. What V1 does

Two read and write paths, deliberately separate:

- **Read** (`get_home_state`). Up to 32 semantic names. Each maps to one entity. The model passes a name from an `enum`; the adapter does one `GET /api/states/<entity_id>` and returns a sealed JSON result: `{ok, name, value, unit}` or `{ok: false, name, error}`. No service call exists in this module.
- **Control** (`control_light`). Up to 8 semantic lights, a completely separate catalogue. The model passes a name and `on` or `off`. The adapter POSTs `light|switch.turn_on|turn_off` for that one entity, ignores the service response body, then reads the state up to three times (now, +0.4 s, +0.8 s). `ok: true` only when the observed state equals the action (`outcome: confirmed`). Otherwise `state_unconfirmed` (with the observed state if it was on or off) or a specific error.

What makes it safe, all of which carries over unchanged:

- Read and control are separate allow-lists. A readable name grants nothing to control. A control entry must say `light:` and the entity domain must be `light` or `switch`.
- Caps: `MAX_READINGS = 32`, `MAX_CONTROLS = 8`. Over the cap is dropped with a warning that names the key, never the value.
- The model never sees an entity id. Tool parameters are `enum`s of semantic names. A dotted name is rejected as `unknown_name`.
- Fixed error vocabularies (`ERRORS` in each module). Results are rebuilt from known keys (`_seal`), so nothing from Home Assistant rides along.
- Value parsing: a bare entity or `number:` must be a plain decimal; `text:` is a short string (≤ 200 characters). Type is never guessed from the domain. `unavailable` and `unknown` are failures, not values.
- URL must be an http(s) origin with no path, credentials, query, or fragment. The token is only ever an `Authorization` header and is checked never to appear in a URL. Response bodies are capped at 64 KB. Error bodies are discarded.
- Tool descriptions carry the identity rules: the full listed name is required, a partial or shared ending does not pick a device, one listed name is not a default, ask one short question when ambiguous, and never treat an earlier result as the current state.
- A system-prompt block (“Home state (history is not the present)”) repeats the fresh-read and no-reading-before-control rules.

## 2. V1 files and functions

Committed V1 sources involved:

| File | What matters |
|---|---|
| `server/brain/home_assistant.py` (463 lines) | `Reading`, `MAX_READINGS`, `ERRORS`, `_HA_KEY`, `_ENTITY_ID`, `_fail`, `_ok`, `_remember_url`, `_origin_ok`, `_state_url`, `_unit`, `_number`, `_interpret`, `urllib_get`, `_semantic_name`, `_parse_reading`, `readings_from_env`, `HomeAssistant` (`get_state`, `from_env`, `names`, `__repr__`), `state_for_tool`, CLI `main` |
| `server/brain/home_control.py` (451 lines) | `Light`, `MAX_CONTROLS`, `VERIFY_PAUSES`, `ERRORS`, `_ALLOWED_DOMAINS`, `_failure`, `_confirmed`, `_seal`, `_allowed_entity`, `_parse_control`, `controls_from_env` (with the read/control entity mismatch warning), `urllib_post`, `_classify`, `HomeControl` (`set_light`, `_verify`, `_observe`), `set_light_for_tool` |
| `server/brain/thinking.py` | `_spoken_name`, `_identity_counterexamples`, `_shared_ending_sentence`, `_device_identity`, `_home_description`, `_home_tool`, `_light_description`, `_light_tool`, `_semantic_catalogue`, `build_tools(names, lights)` |
| `server/brain/main.py` | `_home` / `_control` singletons, `open_home`, `open_control`, `configured_tools`, `get_home_state`, `control_light`, `ABILITIES["get_home_state"]`, `configured_actions` (adds `control_light` only when a light exists), startup log of names |
| `server/brain/personality.py` | “Home state” block, and “A live reading from the home is not one of those facts” in the seeing rules |
| `server/brain/config.py` | Comment only. `.env` is loaded into `os.environ`; the token is not copied onto `config` |
| `server/.env.example` | `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN`, `HA_<NAME>`, `HA_CONTROL_<NAME>=light:<entity>` |
| `server/tests/test_home_assistant.py` (633) | Contract, catalogue, transport, production boundary, smoke CLI |
| `server/tests/test_home_control.py` (922) | Catalogue, schema, perform/verify, transport, boundary, brain loop, wiring |
| `server/tests/test_get_home_state.py` (593) | Schema, routing, failures, brain loop, ability wiring, prompt does not leak readings |

Runtime dependency: `certifi` (already in V2 `requirements.txt`). Everything else is the standard library.

## 3. V1 and V2 differences that matter

V2 has no Home Assistant code at all. `rg` for `home_assistant`, `HA_CONTROL`, `get_home_state`, `control_light` finds nothing in V2.

| Area | V1 | V2 | Effect on the port |
|---|---|---|---|
| Tool list | `RobotBrain(actions, tools)`; `build_tools()` fixed at start | `RobotBrain(actions, extra_tools=callable, quick=set)`; base `thinking.TOOLS` plus `extra_tools()` evaluated **every model round** | Home tools go in `extra_tools`, like the computer tools. No change to the tool loop. Live updates come free. |
| Dispatch | `configured_actions()` adds `control_light` only if a light exists | `ABILITIES` is a static dict. Computer actions are always registered and refuse when switched off | Register both home actions always. The adapter returns `unknown_name` / `not_configured` when not allowed. The tool is only advertised when allowed. |
| Action return | `(text, None)` | `(text, img)` or `(text, img, done)`; exceptions become “could not do that” | V1 `state_for_tool` / `set_light_for_tool` already return `(text, None)`. Compatible. Not added to `quick` (the model must hear the result). |
| System prompt | Rocky prompt includes the Home state block | `personality.SYSTEM_PROMPT` is built once at import (Rocky or a dashboard character, plus personal context). Read on every request | A live on/off cannot add or remove a static block. See decision 2. |
| Configuration | `.env` only, read once; restart to change | `server/settings.json` (gitignored), one schema in `settings.py`, written by the launcher, applied by `apply_settings()` on start and on `/api/settings_reload` | Home config becomes a settings section. Secrets need their own store (below). |
| Dashboard | None for HA | `dashboard.html` served by `launcher.py` (stdlib only, runs with the brain stopped) | Test and discovery must run in the launcher, so the HA module must not need non-stdlib imports at import time. |
| Secrets | `.env` | `.env` for API keys, read by `config.py` and freshly by `launcher.env_value()`. No secret in `settings.json`; `GET /launcher/settings` returns the whole file | The token must not go into `settings.json`. |
| Threads | Tools on the brain worker thread | Same, plus settings applied on the event loop | Swap the adapter pair atomically under a lock (as `computer.Computer` does). |

## 4. How the Computer panel works

This is the pattern to copy.

1. **Schema.** `settings.SCHEMA["computer"]` has `control` (bool, default false), `apps`, `bookmarks`, `shortcuts` (str with max length), `confirm_shortcuts`. Structured lists are stored as text: `bookmarks` is “Name = https://…” per line.
2. **Validation on save.** `settings.clean_value()` checks kind and length. For `computer.bookmarks` it calls `computer.parse_bookmarks()`, which raises `ValueError` naming the bad line. The launcher returns that as HTTP 400 and the page shows a toast.
3. **Persistence.** `settings.update()` merges, validates, and writes atomically (temp file then rename). Unknown keys are rejected. Bad values in an existing file fall back to the default on load.
4. **Live apply.** `POST /launcher/settings` saves, then `reload_brain()` POSTs `/api/settings_reload` to a running brain. `main.apply_settings()` calls `computer.controller.configure(s["computer"])` under the controller’s lock. The page shows “saved” or “saved · applies when the server runs”.
5. **Model tools.** `computer.tools(controller)` is the brain’s `extra_tools`. Switched off, it offers one small `use_computer` tool that says how to switch it on. Switched on, it offers only allow-listed names as `enum`s. Every URL and command is built in the module, never by the model.
6. **Dashboard.** One `<fieldset class="card">` with a legend and a `data-saved` tag. Inputs named `section.key` autosave on change through `save()`. Extra helpers (Pick from my Shortcuts) call a launcher GET and append to the text field, then `save()`.
7. **Sensitive data.** Spotify keys stay in `.env`. The page only learns `spotify_key: true/false`. Status to the page (`computer.controller.status()`) is `{on, last}`.

## 5. Proposed V2 architecture

Smallest change: port the two V1 modules nearly verbatim, add one small V2-shaped controller, and touch `main.py`, `settings.py`, `launcher.py`, `dashboard.html` in the same way the computer feature does.

### New modules (ported)

- `server/brain/home_assistant.py`. From V1, with these changes only:
  - Import `certifi` lazily inside `urllib_get` (as `computer._fetch_json` does), so the launcher can import the module.
  - Add `readings_from_text(text) -> (dict[str, Reading], warnings)` for the settings format below. It reuses `_parse_reading` and the 32 cap. Semantic names use the same rule as V1, written in lowercase (`pool_temperature`, equivalent to `HA_POOL_TEMPERATURE`).
  - Add read-only helpers for the dashboard: `check_connection(url, token)` → `GET /api/` (200 ok, 401/403 `authentication_failed`, others mapped like `get_state`); `list_entities(url, token)` → `GET /api/states`, returning only `entity_id`, `friendly_name`, `state` (truncated), with its own larger body cap (for example 4 MB) and a result limit.
  - Drop `from_env` from the production path (see migration). Keep `readings_from_env` only if the optional importer is approved.
  - Adapt the CLI smoke test to read the dashboard config instead of `.env`, or drop it.
- `server/brain/home_control.py`. From V1, with lazy `certifi` and `controls_from_text(text)`. The service path, verification, `_seal`, domains, and caps stay exactly as they are.

### New module (V2 glue)

- `server/brain/home.py`, shaped like `computer.py`:
  - `Home.configure(section: dict, token: str)` builds a fresh `HomeAssistant` and `HomeControl` and swaps them in under a lock. Disabled means empty catalogues.
  - `tools(home)` returns `[]` when disabled or nothing is allowed. Otherwise it returns the V1 `_home_tool(names)` and/or `_light_tool(lights)`. The V1 description builders (`_device_identity` and friends) move here verbatim from V1 `thinking.py`, so V2’s `thinking.py` is not reshaped.
  - `ACTIONS = {"get_home_state": ..., "control_light": ...}`, always registered. They read the current adapter pair, so a removed name returns `unknown_name` even if an old message in history mentions it.
  - `status()` returns `{on, readings: [names], lights: [names], token_set}`. No URL, no token, no entity ids.
  - `load_token()` / `save_token()` / `clear_token()` (see storage).

### Edits to existing V2 files

- `main.py`: `from . import home`. Merge `home.ACTIONS` into `ABILITIES`. In `apply_settings`, call `home.controller.configure(s["home_assistant"], home.load_token())`. `extra_tools=lambda: [*computer.tools(...), *home.tools(home.controller)]`. Add `"home": home.controller.status()` to `console_state`. Log names at start, as V1 does.
- `thinking.py`: only if decision 2 picks the prompt hook. Add an optional `extra_prompt: Callable[[], str]` to `RobotBrain` and append its text to the system message per request. About five lines.
- `settings.py`: new `home_assistant` section (below). Validation calls the HA parsers, like `computer.bookmarks`.
- `launcher.py`: four small routes (below). After any token or config change, call `reload_brain()`.
- `dashboard.html`: one new card after Computer.
- `.gitignore`: the token file.

No firmware change. No Wi-Fi change.

## 6. Dashboard and storage

### Settings section (non-secret, `server/settings.json`)

```text
home_assistant:
  enabled   bool   default false
  url       str    max 200   validated: http(s) origin, no path, no credentials, no query
  readings  str    max 4000  one per line:  pool_temperature = number:sensor.pool_temperature
                             or              printer_state = text:sensor.printer_state
  lights    str    max 1000  one per line:  desk_lamp = light:switch.desk_lamp
```

Text lines keep V1’s value format and prefixes (`number:`, `text:`, `light:`) exactly, keep `settings.py` scalar-only like `computer.bookmarks`, and make validation reuse V1’s parsers. On save, unlike V1’s `.env` (which silently dropped extras), more than 32 readings or 8 lights, a bad name, a bad entity, a non-`light`/`switch` control entity, or a duplicate name is a 400 naming the line. A read entry and a control entry with the same name but different entities is allowed, as in V1, and shown as a warning.

### Token (secret)

Recommendation: a dedicated file, `server/home_assistant.token`.

- Gitignored. Written by the launcher only, atomically, mode `0600`.
- Never returned by any GET. `GET /launcher/settings` gains `home_assistant_token: true|false` only.
- Never logged. `HomeAssistant.__repr__` already shows only the URL.
- Never in model context: tools take names only, results are rebuilt from fixed keys.
- The brain reads it in `apply_settings`, so replacing or clearing it applies live.

Why not the alternatives:

- `settings.json`: returned in full to the page; anyone with the dashboard open could read it.
- Writing `server/.env` from the dashboard: rewrites a hand-edited file holding other keys. The launcher’s `LAUNCH_ENV` also wins over the file, so a token set in the shell would silently override the dashboard.
- macOS Keychain: better at-rest protection, but Mac-only and adds a dependency or a subprocess. Not needed for the same local trust level V1’s `.env` already has. It can replace the file later behind `load_token` / `save_token`.

### Launcher routes

| Route | Does | Never |
|---|---|---|
| `POST /launcher/home_assistant/token` `{token}` or `{clear: true}` | Writes or deletes the token file, then `reload_brain()` | Echo the token |
| `POST /launcher/home_assistant/test` | `check_connection` with the saved URL and token (or the URL in the form and the saved token). Returns `{ok, error?, message}` | Return the token or a response body |
| `GET /launcher/home_assistant/entities?q=…&for=read\|control` | `list_entities`, filtered by text. For `control`, only `light.*` and `switch.*`. Up to 50 rows of `{entity_id, friendly_name, state}` | Write settings or grant anything |
| `GET /launcher/settings` (existing) | Adds `home_assistant_token: bool` | Include the token |

All are behind the launcher’s existing local-host and console guards. No new brain endpoint.

### Card layout

```text
Home Assistant                                              saved
[ ] Let Rocky use Home Assistant
    He can read only the entities you list, and turn only the lights you list on or off.
    Anyone talking near him can ask too.

Connection
  URL        [ http://homeassistant.local:8123 ]
  Token      [ ••••••• (saved — type to replace) ]  [Save token] [Clear]
  [Test connection]   Connected · API running   |   Rejected token (401)   |   Can't reach that address

He may read  (5 of 32)
  pool_temperature    sensor.pool_temperature        number   [Edit] [Remove]
  ...

He may turn on and off  (1 of 8)                 Separate from reading
  desk_lamp    switch.desk_lamp  light   [Edit] [Remove]

Find an entity
  [ search… ]
  Pool Temperature · sensor.pool_temperature · 27.5       [Add as reading]
  Desk Lamp · switch.desk_lamp · off        [Add as reading] [Add as light]
```

- Rows are rendered from the `readings` / `lights` text. Edit, Remove, and Add rewrite that text and call the existing `save()`.
- Add asks for the semantic name, prefilled from the friendly name (lowercase words joined by underscores), and the type (number or text) for readings.
- “Add as light” appears only for `light.*` and `switch.*`. Adding to one list never touches the other.
- Discovery results only show. Nothing is granted until the person presses Add and the save succeeds.

## 7. Configuration migration

V2’s `server/.env` currently has no Home Assistant keys. V1’s real `.env` was not opened.

Recommended:

1. The dashboard is the only source of truth. The V2 brain does **not** read `HOME_ASSISTANT_*`, `HA_*`, or `HA_CONTROL_*` from `.env`. If any are present, it logs one line naming the keys (never values) and pointing to the dashboard.
2. Migration is manual and explicit. Paste the V1 URL and token into the card (the token goes to the token file), then add the readings and the light. Discovery makes this quick for the handful of V1 entries. A short “Moving from V1” paragraph in the README maps `HA_POOL_TEMPERATURE=number:sensor.x` to the row `pool_temperature = number:sensor.x`, and `HA_CONTROL_DESK_LAMP=light:switch.y` to the light row.
3. V2 `.env.example` gets no HA keys, only a note that Home Assistant is set up in the dashboard.

Optional (decision 3): a one-time “Import from server/.env” button. It reads V2’s own `.env` on request, shows a preview of names and entity ids (never the token), and writes settings plus the token file only after confirmation. Behaviour is the same afterwards. Skip it unless the manual entry proves annoying.

## 8. Tests

All offline. Fake `fetch` / `post` / `pause` like V1. No real Home Assistant, no real token. Temporary paths for `settings.json` and the token file.

### Reuse from V1 (copied, then adapted)

- `test_home_assistant.py`: `ContractTests`, `TransportTests`, `CatalogueTests` (env mapping becomes text lines), `ProductionBoundaryTests`. Drop or adapt `SmokeCommandTests`, and the `from_env` loads `.env` test.
- `test_home_control.py`: `CatalogueTests` (text form), `PerformTests` (verification, unconfirmed, unavailable, missing, auth, timeout), `TransportTests`, `BoundaryTests`. `SchemaTests` point at `home.tools`. `LoopTests` use V2 `RobotBrain(actions, extra_tools=...)`. `WiringTests` move to `main.ABILITIES` plus `home.tools` instead of `configured_actions`.
- `test_get_home_state.py`: routing, failures, brain loop, “readings stay out of the character prompt”. Wiring tests replace `main._home` / `main._control` with `home.controller.configure(...)`.

### New

- Settings: valid section; bad URL; 33rd reading and 9th light rejected; bad semantic name; bad entity; `light:sensor.x` rejected; duplicate name; `number:` / `text:` / `light:` preserved through save and load.
- Separation: a reading never makes `control_light` accept that name; the control tool’s `enum` comes only from `lights`.
- Live update: configure with a light, then without; the tool disappears from `extra_tools()` and the action returns `unknown_name`. Disabled means no home tools at all.
- Token store: written `0600`; absent from `GET /launcher/settings`, from `console_state`, from logs, and from every tool result; clear removes the file; replace takes effect on reload.
- Launcher: `test` maps 200 / 401 / connection error / timeout; `entities` for `control` returns only `light` and `switch`; discovery does not change `settings.json`.
- `.env` HA keys present: ignored, warning names keys only.
- Whole existing V2 suite still passes (`python -m unittest tests/test_*.py` from `server/`), including `test_settings.py`, `test_computer.py`, and `test_wifi_profiles.py`.

Live Home Assistant testing is a separate, explicitly authorised step after the offline suite passes.

## 9. Implementation order

Each step is small and leaves the suite green.

1. Port `home_assistant.py` and `home_control.py` from V1 `HEAD` with the listed changes (lazy `certifi`, text parsers, read-only discovery and connection check). Port their unit tests.
2. Add `home.py` (controller, `tools()`, `ACTIONS`, status, token file helpers). Port the schema, routing, and loop tests against it.
3. Wire `main.py` (abilities, `apply_settings`, `extra_tools`, status, startup log) and the prompt choice from decision 2. Wiring and live-update tests.
4. Add the `home_assistant` settings section and validation, and the token-file `.gitignore` entry. Settings tests.
5. Add the launcher routes. Launcher tests with fake HTTP.
6. Add the dashboard card (connection, two lists, discovery). Manual check in the browser with the brain stopped and running, against a fake or no Home Assistant.
7. README “Home Assistant” and “Moving from V1” notes; `.env.example` note.
8. Stop. Live test with the real Home Assistant only when authorised: test connection, read one sensor, turn one approved light on and off, ambiguous name asks, unknown name refuses, wrong token reports authentication failure.

Rough size: about 900 lines ported (mostly unchanged), about 250 new lines across `home.py`, settings, and launcher, about 200 lines of dashboard HTML and JS, plus tests.

## 10. Risks and decisions

### Decisions needing approval

1. **Token storage.** Recommended: `server/home_assistant.token`, gitignored, `0600`, launcher-written, never returned. Alternatives: Keychain (Mac-only) or `.env` (rejected above).
2. **Prompt rules.** V1 tested behaviour with the “Home state” block in the system prompt. V2’s prompt is static. Recommended: a five-line `extra_prompt` hook in `RobotBrain` that appends V1’s block (and the “a live reading is not one of those facts” sentence) only while home tools are offered. Alternative: rely on the tool descriptions alone (they already hold most of it) and accept a small behaviour risk.
3. **Migration.** Recommended: manual entry; `.env` HA keys ignored with a key-name-only warning. Optional: an explicit preview-and-confirm import from V2’s `.env`.
4. **List storage format.** Recommended: line text in `settings.json` (V1 value format, matches `computer.bookmarks`). Alternative: structured JSON lists, which need a new list kind in `settings.py`.

### Risks and compatibility

- **Prompt drift.** Characters other than Rocky never had the V1 block. With the hook, every character gets the same home rules. Without it, ambiguity and fresh-read behaviour may differ from V1 until tested live.
- **History after a live change.** Earlier tool calls stay in history when a tool is removed. Dispatch already rejects removed names (`unknown_name`). Some providers are strict about tool calls for tools not in the current list; the computer on/off switch already relies on the same behaviour.
- **Discovery size.** `/api/states` can be large on big installs. It needs its own cap and runs only in the launcher on request, never per question.
- **Same trust level as V1.** The token is plaintext on local disk, as in V1’s `.env`. Anyone with the dashboard open can make Rocky read or switch approved entities, and anyone near the mic can ask. The card says so, as the Computer card does.
- **Launcher imports.** The launcher runs without brain dependencies. The ported modules must keep `certifi` lazy and stay standard-library at import.
- **No broadening.** No generic service calls, no new domains, no brightness or colour, no scenes, scripts, locks, covers, or climate. Same 32 / 8 caps. A readable entity never becomes controllable.

## 11. Implementation status

Approved decisions, as built: (1) token file `server/home_assistant.token`; (2) V1’s prompt rules added only while a home tool is offered; (3) manual migration, `.env` keys ignored and named in one log line; (4) text lines in `settings.json`.

### What was built

| File | Change |
|---|---|
| `server/brain/home_assistant.py` | New. Ported from V1 with the read contract unchanged. Added: lazy `ssl_context()`, `check_url`, `readings_from_text`, read-only `check_connection` (`GET /api/`) and `list_entities` (`GET /api/states`). Removed: `.env` loading, the CLI. |
| `server/brain/home_control.py` | New. Ported from V1 with the control contract unchanged. Added: `controllable`, `controls_from_text`, `mismatches`. Removed: `.env` loading. |
| `server/brain/home.py` | New. Token file helpers, the `Home` controller (adapters swapped under a lock on every settings apply), V1’s tool builders and prompt rules, `tools()`, `ACTIONS`. |
| `server/brain/thinking.py` | `RobotBrain(system_prompt=…)`: a callable applied to the base prompt each round. While a home tool is offered, that round is held until it is known whether `get_home_state` or `control_light` was called; that lead-in stays in the transcript and is not spoken (section 14). |
| `server/brain/main.py` | `ABILITIES` gains both home actions; `apply_settings` configures the controller; `extra_tools` adds `home.tools`; `console_state` adds `home` status (no token); startup names ignored `.env` keys. |
| `server/brain/settings.py` | `home_assistant` section (`enabled`, `url`, `readings`, `lights`) validated with V1’s parsers. |
| `server/launcher.py` | `POST /launcher/home_assistant/token`, `/test`, `/entities`; `GET /launcher/settings` adds `home_assistant_token: bool`. |
| `server/brain/dashboard.html` | Home Assistant card. |
| `.gitignore` | Token file and its temporary files. |
| `README.md`, `server/.env.example` | Setup, moving from V1, privacy notes. |
| `server/tests/test_home_assistant.py`, `test_home_control.py`, `test_get_home_state.py`, `test_home_settings.py` | Ported and new. The full V2 server suite, including these, is 242 tests. |

Unchanged from V1: both `ERRORS` vocabularies, `_seal` / `_ok` / `_fail`, value parsing, URL checks, 64 KB response cap, post-action verification (`0.4 s`, `0.8 s`), the tool descriptions and identity rules (checked word for word against V1 when V1 is beside V2), and the “Home state” prompt block and “live reading” sentence (also checked word for word).

### Deviations from the plan

- **Test and discovery are `POST` with the console guard**, not `GET`. Both reach out to Home Assistant with the token, so they get the same guard as the other launcher actions.
- **Test and discovery use only the saved URL.** A URL in the request is ignored. The card saves a just-typed URL before testing or searching.
- **Text caps** are 6000 characters for readings and 1500 for lights (plan: 4000 and 1000), so 32 and 8 lines with long entity ids fit.
- **Prompt hook is a transform**, `system_prompt(base) -> str`, not an append. The “live reading” sentence goes after “Math, facts, advice, chat: just answer, from what you know.”, as in V1’s seeing rules, and the “Home state” block goes before the emotion-tag rule, as in V1. If an anchor is missing (another character), it is appended instead. Never added twice.
- **Card position**: end of the left column, after Vision, not after Computer, to keep the columns balanced.
- **V1’s CLI smoke command** (`python -m brain.home_assistant`) was not ported; the card’s Test connection replaces it. Its tests were dropped.
- **Lazy `certifi`** falls back to the system CA store if `certifi` is not installed (the launcher has no brain dependencies).
- **Discovery** returns at most 50 rows (with the total matched), from at most 5000 entities and an 8 MB body. Rows are `{entity_id, name, state, unit, controllable}`.
- **Edit as text**: a collapsed textarea pair under the card, for pasting many V1 lines at once. It saves through the same validation.
- **A saved list that fails to parse** (possible only by hand-editing `settings.json`) is logged and treated as empty: nothing is granted.

Live reads, light control, the dashboard Add fix, and the speech fix are recorded in sections 12–14. They are done.

## 12. Live acceptance

Automated result at the time of the first live attempt: 234 tests, mocked Home Assistant only, no contact with the real instance. The finished suite is 242 (section 14).

Live check on 27 Oct 2026, `v2-dev`, uncommitted. The launcher was started from this session and the brain was started with the dashboard's **Start server**. Nothing was committed. V1, firmware and Wi-Fi were not touched. The token file was not read or printed. No light was switched.

### Before testing

- Branch is `v2-dev` at `807a62b`, with the Home Assistant work uncommitted on top. The Stage 1 proof files are still the only firmware changes.
- `server/settings.json` is gitignored. Starting the brain does not write it: `apply_settings` only reads. After the brain was up, the file's checksum matched the checksum taken before start. Switching Home Assistant off and back on rewrote that one flag and then restored the same bytes. Clock, weather, screen, computer control, camera rotation and the rest were unchanged.
- The 09:38 checksum change during the first automated run did not come back on a second full run or on any single test file. Every settings test points `settings.PATH` at a temporary file. The likely writer was a Rocky process still running the previous code: its `persist()` rewrites the whole file from a schema with no `home_assistant` section, which refreshes the file without resetting the other settings and without keeping the new section. No such process was running at the start of this check. The current code writes the new section with everything else, so a restart does not drop it.
- `server/home_assistant.token` is gitignored, mode `0600`, and was not returned by `GET /launcher/settings` (`home_assistant_token` is a boolean). The token field on the page stays empty and the hint says a token is saved and is never shown.

### Dashboard

| Check | Result |
|---|---|
| Panel loads | Pass. Settings shows the Home Assistant card. |
| Enable switch | Pass. Off logged `home assistant: off` at once. On again logged `home readings: none` and `home lights: none`. The address stayed. |
| URL saves and reloads | Pass. After a full page reload the saved address was back in the field. It was not retyped. |
| Token not returned | Pass. Empty password field, hint only, boolean flag in the settings response, no long secret in that JSON. |
| Test connection | Fail. The card said "Couldn't reach that address." |
| Discovery, search, filter | Not completed. Search showed the same "Couldn't reach that address." The read and light lists stayed empty, so the failed search granted nothing. |
| Add reading / add light / remove | Not run. There was no entity list to add from. |
| Settings survive restart | Pass. See above. |

### Why the connection test failed

Home Assistant is up. From this Mac, `curl` and `/usr/bin/python3` reach it and get HTTP 401 with no token, which is the right answer for an unauthenticated request. The launcher and the brain use Homebrew Python 3.13 from `server/.venv`. That interpreter connects to the public internet and to localhost, and fails on the local Home Assistant address with `OSError` errno 65, "No route to host". The same failure happens with a fake token, so this is not a rejected token and not a bad address.

That errno, only on a local address, only from the third-party Python, is macOS Local Network privacy. The smallest fix is to allow it, not to change the integration:

1. System Settings → Privacy & Security → Local Network.
2. Turn on the app that owns this process. Started from Cursor, that is Cursor. Started from Terminal, that is Terminal. Python itself may be listed.
3. Stop the launcher and start it again. A process that already failed stays blocked after the switch is turned on.
4. Press **Test connection** again. The next result should be "Connected: Home Assistant accepted the token." A 401 message would mean the saved token, not the network.

No integration change was made for this. The next step was the Local Network switch, then a retry.

### Resolution

Local Network access was turned on for the app that starts Rocky. Test connection and discovery then succeeded. Configuration, reads, and light control are in sections 13 and 14.

## 13. Live acceptance update — Add workflow fix

After Local Network access was enabled, the connection test and discovery succeeded. Configuration entered manually through **Edit as text** was saved unchanged; Rocky then read the approved pool-temperature sensor and controlled the approved desk lamp end to end. This is live acceptance of the configured read and on/off control paths. No credentials are recorded here.

Discovery's **Add** dialog failed with a name error on the new line (`line 1` with empty lists, `line 2` with one saved entry), even though the typed name was valid. Placeholders and blank lines were not the cause. A save-blocked capture of the real browser request showed the new line as `$Rocky = light:switch.…`: the launcher serves the page with every `{name}` replaced by the robot's name, and the Add handler built the line with the template literal `${name} = ${value}`, which became `$Rocky = …`. Server validation rightly refused it.

Fix: the handler's local variable is now `label`, so no `{name}` appears in that code. Add and Edit also read their own textarea (Readings or Lights) rather than a cached copy. Validation and permissions are unchanged. After the fix, the same capture sent the typed name on a new line (`second_lamp = light:switch.example` for a light, `lamp_state = text:switch.example` for a reading), each changing only its own list.

Regression tests: the served page no longer produces `$<robot name> = `, and the dashboard source has no `${name}`; with one reading and one light already saved, the broken request is still refused as `line 2`, and the corrected request saves with both existing entries kept. The saved URL, token file and allow-lists were not changed by the fix.

## 14. Speech and final live confirmation

V2 speaks a sentence as soon as it is finished, before it knows whether a tool call is still coming. V1 kept that pre-tool text in the transcript and did not speak it. The Home Assistant instructions were written for that silence, so the model narrated the decision and V2 said it aloud. A phrase filter and an extra prompt sentence did not stop it: the narration is ordinary text, and the next wording was different.

The fix is only in `server/brain/thinking.py`. While `get_home_state` or `control_light` is among the tools offered, the round is held until the stream ends. If that round called either tool, including a mixed round, the lead-in stays on the assistant tool-call message and is not spoken. The tool runs, and the following round's answer is spoken once. A round that does not call a home tool is spoken after the hold. When those tools are not offered, sentences are spoken as they complete. Computer, `look`, `dance` and `track_face` still say their line and still end the reply. There is no extra model call and hidden reasoning stays off. V1's safety text is unchanged.

With home tools offered, the first word waits until that round finishes. A reply is already capped at 3 sentences and 200 tokens. A local probe with a 250 ms tail measured about 260 ms to the first sentence with the tools offered, and under 1 ms with them absent. A home-tool round adds no spoken lead-in; the answer still comes from the following model call, which was already required.

Final live check on 27 Oct 2026, on `v2-dev`, after this speech change: dashboard configuration and entity discovery, adding and removing approved entities, live sensor readings, physical light control, spoken answers without internal tool narration, and ordinary conversation and quick actions. The full V2 server suite passed: 242 tests, mocked Home Assistant only. V1, firmware and Wi-Fi were not modified for this integration. The Stage 1 setup-AP proof stays uncommitted. No URL, token or private entity id is recorded here.