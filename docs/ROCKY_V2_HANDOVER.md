# Rocky V2 — development handover

Wi-Fi provisioning on the V2 robot (XIAO ESP32-S3 Sense) is closed at the Stage 3B.1 checkpoint. Home Assistant V2 is complete and pushed (`7407574`). The next development task is undecided; see [Next development focus](#next-development-focus).

Repository: `/Users/rus/Development/Python/desk-robot-v2`. The V1 tree (`/Users/rus/Development/Python/desk-robot`) is a different project and stays separate.

## Current working checkpoint

| | |
|---|---|
| Branch | `v2-dev`, tracking `origin/v2-dev` |
| Stage 3B.1 | This commit, parent `93ab27f` — *Fix Wi-Fi provisioning reliability edge cases* |
| Stage 3A | `93ab27f515395a09ed9092375cf4c3477cdebe15` — *Add captive Wi-Fi provisioning and multi-network failover* |
| Stage 2 | `f2e3fa7` — *Add persistent Wi-Fi profiles and connection fallback* |
| Casey's `main` | Do not touch. Local `main` tracks `upstream/main`. No merge, rebase, or push to `main` without explicit approval. |

The Stage 3A source is what was physically tested. After that commit, `pio run -e xiao_esp32s3` succeeded:

- RAM: 22.8% (74,552 / 327,680 bytes)
- Flash: 33.4% (1,117,061 / 3,342,336 bytes)

Host policy test at that checkpoint: `python3 -m unittest tests.test_wifi_profiles` from `server/` (1 test, OK). It compiles `firmware/test/net_policy_host.cpp` against `firmware/src/net_policy.h`.

`firmware/include/secrets.h` is private and gitignored. It defines `SETUP_AP_PASS` for this machine. Do not print it, commit it, or copy it into the handover.

## Stage 3B.1 — accepted

Completed on 27 October 2026, on top of `93ab27f`. Host tests passed. The production image built, was uploaded, and basic physical acceptance on Rocky succeeded. The current behaviour is accepted. Further edge cases, if they show up in real use, can be handled later. This does not mean every edge case was reproduced on the hardware.

Host check, from `server/`: `python3 -m unittest tests.test_wifi_profiles` — 2 tests, OK (policy binary plus a check that the firmware call sites use the new helpers).

Production build that was uploaded: `pio run -e xiao_esp32s3`.

- RAM: 22.8% (74,552 / 327,680 bytes), same as the Stage 3A image
- Flash: 33.5% (1,117,573 / 3,342,336 bytes)

Accepted behaviour:

- **A.** Idle no longer treats a cleared down-timer as “down for the whole uptime”. A station that is associated and has an IP is adopted (`Phase::Up`, failed-round flags cleared, last-ok and brain host updated from the SSID, or from the attempt still in flight if the SSID has not been reported). Setup and another `WiFi.begin` are not started over that station. A full round plus about 60 seconds still opens Rocky-Setup when the station is actually down. Brain/WebSocket loss still does not.
- **B.** `wifi forget` accepts only a whole non-negative slot number. `abc`, `1abc`, `-1`, a blank argument, and an integer that does not fit in 32 bits do not become slot 0 and do not write NVS. Last-ok is still refused. A valid other slot still compacts namespace `wifi` only.
- **C.** Saved SSIDs are HTML-escaped on the portal page (`&`, `<`, `>`, `"`, `'`). The bytes in NVS stay as stored. Passwords, `SETUP_AP_PASS`, and `ROBOT_TOKEN` are still not shown or logged.
- **D.** After submit the page heading is “Connecting…”, not “Saved”. The text says the network is saved only after it connects, that the phone may leave Rocky-Setup during the attempt, and that a failure brings the portal back. The button says “Test this network”. There is still no second success page, no AP+STA mode, and no scan while the portal is up. Rollback and validation are unchanged.

Deferred, not part of this acceptance: once Rocky-Setup is already up, a saved network that returns is still not joined automatically. That remains unsupported. See [Future enhancements — deferred](#future-enhancements--deferred).

## Completed Wi-Fi functionality

Station Wi-Fi is a small state machine in `Net` (`Idle`, `Join`, `Up`, `Setup`). The brain WebSocket is separate. The main loop calls `net.update()`, then opens the brain socket only when `net.staUp()` is true and that profile has a brain host. Otherwise it calls `brainLink.wifiDown()`.

### Source map

| Piece | File |
|---|---|
| Pure rules (host-testable, no Arduino) | `firmware/src/net_policy.h`, `firmware/test/net_policy_host.cpp`, `server/tests/test_wifi_profiles.py` |
| NVS, join loop, portal commit | `firmware/src/net.cpp`, `firmware/src/net.h` |
| Captive portal (AP, DNS, HTTP) | `firmware/src/setup_portal.cpp`, `firmware/src/setup_portal.h` |
| USB `wifi` / `wifi forget`, when the brain socket runs | `firmware/src/main.cpp` |
| WebSocket only; disconnect does not call `Net` | `firmware/src/link.cpp` |
| Timeouts | `firmware/include/config.h` — `WIFI_JOIN_MS` 20 s, `WIFI_RETRY_MS` 20 s, `WIFI_SETUP_AFTER_MS` 60 s |
| Credential template | `firmware/include/secrets.h.example` (`SETUP_AP_PASS`, 8+ characters) |
| User-facing setup notes | `README.md` |

### Profiles

- Up to four profiles (`NET_MAX_PROFILES`) in NVS namespace `wifi` only. Keys are `migrated`, `last`, `n`, and per slot `s0`–`s3`, `p0`–`p3`, `h0`–`h3`.
- Namespaces `home` (clock) and `face` (face pack) are not opened by this code.
- First boot with empty NVS and a seed SSID in `secrets.h` copies that seed into slot 0 and sets `migrated`. Later flashes do not copy again. An empty list after `migrated` is set stays empty; the portal is how a network is added.
- `WiFi.persistent(false)` so the Arduino Wi-Fi stack does not keep its own copy of the credentials.
- Each profile has its own brain host. The port is the compile-time `BRAIN_PORT` (default 8765), shared by every profile.
- Last-ok is the slot that most recently associated and got an IP. The next round tries last-ok first, then the other slots. A slot that failed is skipped until the round ends. A successful fallback writes the new last-ok and therefore the brain host that `main` will dial.

### Joining

- Direct `WiFi.begin` of a saved profile. No scan in the production join path.
- One attempt at a time, nonblocking. `WIFI_JOIN_MS` (20 s) then the next saved profile.
- After every saved profile has failed once, `triedAll` is set. Setup starts only when that is true and the station has been down for about `WIFI_SETUP_AFTER_MS` (60 s), measured from the first disconnect (`downSince_`), not from the end of the round. A round still in progress does not open setup at the 60 s mark.
- Between rounds the machine waits `WIFI_RETRY_MS` (20 s), clears the skip flags, and tries again, unless the 60 s gate has already opened setup.
- No saved profiles: Rocky-Setup starts immediately.

### Captive portal

- WPA2 access point `Rocky-Setup` at `192.168.4.1` (channel 1, up to four stations). Password is `SETUP_AP_PASS` from `secrets.h`. It is never printed.
- DNS wildcard to that address, plus the usual captive paths (`/hotspot-detect.html`, `/generate_204`, `/ncsi.txt`, and so on). iPhone often needs Safari at `http://192.168.4.1` when it does not show a login sheet.
- Form fields: SSID (1–32 printable characters), password (empty for an open network, or 8–64 characters), brain host (IPv4 or hostname).
- The page lists saved SSIDs. It does not list passwords or `ROBOT_TOKEN`.
- Same SSID updates that slot. A new name uses the next free slot. A fifth distinct name is rejected; nothing is overwritten.
- Submit is provisional RAM only. `netCommitPortal` writes NVS, advances `count`, and moves last-ok only after `WL_CONNECTED` (association and an IP). A failed new name is dropped from RAM and does not consume a slot. A failed update restores the previous password and brain host in RAM; last-ok stays put; NVS was not written.
- Leaving the AP uses `WiFi.disconnect(false, false)`, `WiFi.softAPdisconnect(false)`, and `WiFi.mode(WIFI_STA)` / `WIFI_AP`. It does not call `disconnect(true)`, `softAPdisconnect(true)`, or `WiFi.mode(WIFI_OFF)`. Those deinit the driver and the next `esp_wifi_init` returns `ESP_ERR_WIFI_STOP_STATE` (12308, "netstack cb reg failed"). `netIfaceEnterSetup` / `netIfaceLeaveSetup` lock that rule for the host test.

### Brain down is not setup

A WebSocket drop sets `brainConnected` false, stops the mic and camera stream, and may put the face to sleep. It does not call `Net`. While `WiFi.status() == WL_CONNECTED` and the phase is `Up`, `Net::update` returns without considering setup. `netWantSetup` returns false when its station-up argument is true. Stopping the brain server leaves Rocky on the LAN.

### USB

- `wifi` — profile count, phase, IP, SSIDs, brain hosts, last-ok. No passwords. In setup it also prints station count and page hits.
- `wifi forget <slot>` — drops a saved slot that is not last-ok, compacts the list, and writes namespace `wifi` only. It does not erase the chip. It is not run automatically. A non-numeric argument is rejected instead of being read as slot 0.

## Verified hardware tests

Physically verified on the XIAO ESP32-S3 Sense against the Stage 3A source. The final USB unplug was a deliberate power cycle.

Passed:

- Joins the existing `Rocky-Test` network and the brain at `192.168.2.1`.
- When no saved network is available, `Rocky-Setup` starts on its own.
- An iPhone joins the WPA2 setup AP and opens the captive portal at `192.168.4.1`.
- Provisioned `The_Octopod_EXT_2G` through the portal, brain `192.168.0.6`. The home network assigned `192.168.0.101`. The profile was stored in slot 1 and the brain connected.
- A failed provisional profile was not left in NVS.
- Existing profiles stayed put, and the four-profile limit was enforced.
- AP to STA completed without the previous 12308 network-stack error.
- Automatic failover between `Rocky-Test` and `The_Octopod_EXT_2G` when the preferred network went away. Last-ok and the matching brain host updated.
- With both saved networks unavailable, Rocky tried both before starting `Rocky-Setup`.
- After a deliberate power cycle, Rocky rejoined an available saved network.
- Camera, microphone, OLED, servos, face settings, and normal conversation still worked.

Still not supported: automatic reconnection to a saved network while `Rocky-Setup` is already up. In `Phase::Setup`, `Net::update` only serves the portal and applies a submit. It does not scan or retry saved profiles. Basic acceptance of 3B.1 did not claim otherwise. See [Future enhancements — deferred](#future-enhancements--deferred).

## Stage 3B.1 — what was fixed

All four items below are done and accepted with the basic hardware run. The paragraphs record the bug and the rule that shipped. Host tests cover the decision logic. The robot run confirmed the flashed image behaves correctly in ordinary use. It did not force every rare timing or character case.

### A. Late Wi-Fi association recovery — done

A join that is not `WL_CONNECTED` at the 20 s check goes through `onJoinLost`. That path does not disconnect the radio. If it was the last profile in the round and the 60 s gate is not met yet, the phase becomes `Idle` with `triedAll_` set and `joining_` cleared.

On a later loop, if the station has associated anyway:

1. `noteUp()` sets `downSince_` to 0.
2. The `Idle` branch calls `netWantSetup` with the station-up argument hardcoded to `false`, and `downForMs` equal to `nowMs - downSince_`. With `downSince_` at 0 that difference is `millis()`.
3. After the chip has been up for about a minute, `triedAll && downForMs >= 60000` is true, so `startSetup()` runs and tears down a station that just came up.

The `Join` branch itself is fine: it checks `WiFi.status()` before the timeout and adopts a connection that is already up. The hole is the idle gap after a timeout. A brain disconnect while the phase is already `Up` does not use this path.

Required:

- Recheck the real station state (associated, and an IP) before starting setup.
- Adopt a late success: phase `Up`, `staUp_`, clear `triedAll_`, and update last-ok / brain host for the profile that actually joined. `joining_` is already `-1` here, so the SSID has to be matched against the saved profiles.
- Never tear down a working station to open setup. The idle retry (`joinNext` → `WiFi.disconnect` + `WiFi.begin`) must not run over a live association either.
- Add a host regression test for the decision. The Arduino call stays in `net.cpp`; the rule belongs in `net_policy.h` next to `netWantSetup`, which is what `tests.test_wifi_profiles` compiles.

### B. Strict `wifi forget` argument validation — done

`main.cpp` handles `wifi forget` by `arg.substring(6)` and `String::toInt()`. A missing argument is already rejected. Anything `toInt()` cannot parse, including `forget abc`, becomes 0. If slot 0 is not last-ok, that drops slot 0. `netCanForget` still refuses a negative index, an index past `count`, and last-ok. The bug is the parse, not the last-ok check.

Required:

- Strict integer parsing and bounds checking.
- Reject missing, malformed, negative, and out-of-range arguments.
- Never treat invalid input as slot 0. Leading digits with trailing junk (`1abc`) should be rejected too; `toInt()` would accept them as 1.
- Keep the existing last-ok protection and the “namespace `wifi` only” behaviour.
- Add a regression test. Parsing can be a pure function covered by the host test; `forgetSlot` stays the NVS side.

### C. Captive portal SSID HTML escaping — done

`SetupPortal::serveForm` appends each saved SSID into the HTML with no escaping (`firmware/src/setup_portal.cpp`). An SSID containing `<`, `&`, or quotes breaks the markup and can inject markup. The form’s error strings are fixed text. Passwords are not rendered.

Required:

- Escape every untrusted value that is written into HTML.
- Cover `<`, `>`, `&`, quotes, and a slash in a host test. The page builder is Arduino; a pure escape helper next to the other policy functions is what the existing Python test can compile.
- Keep passwords and `ROBOT_TOKEN` off the page and off serial.

### D. Accurate provisioning feedback — done

The submit response is the static page whose heading is `Saved` (`kThanks` in `setup_portal.cpp`). The paragraph under it says Rocky is still trying that network. Serial and NVS match the paragraph: the profile is stored only after association and an IP, in the `Join` success branch of `Net::update`. The heading is ahead of that.

Required:

- On the page the phone sees at submit time, say that Rocky is connecting or testing the network, not that it is already saved.
- Report the credentials as saved only after association, an IP, and a successful NVS write.
- Keep the error text understandable on an iPhone (validation, full list, bad password, Rocky-Setup coming back).

Constraint: that submit page is served while the phone is on `Rocky-Setup`. As soon as the station join starts, the phone loses `192.168.4.1`. A second “saved” page on the AP is not reachable after a successful join. Serial already logs the save. If the join fails, Rocky-Setup comes back and the form is the right place to explain the retry. Do not add AP+STA, a LAN status page, or any other channel as part of this wording fix.

## Future enhancements — deferred

Not required before other development. Do not implement from this list unless a later session chooses one. The notes are questions and trade-offs, not a chosen design. The known limit that still matters in daily use: once Rocky-Setup is already up, Rocky does not automatically retry saved networks.

**Ask for setup without USB.** Today setup starts only when there is nothing to join, a portal attempt fails, or the 60 s gate trips. A person who is already on a good LAN and wants to add a network has no on-robot control for that. Voice or a brain command needs the socket, so it only works when Wi-Fi and the brain host are already right. A local button would work when they are not. GPIO0 / BOOT is not available: within five seconds of power-up it is assembly mode (servos centre, screen shows ASSEMBLY), and it is the ROM bootloader button. See `docs/wiring.md`.

**Wi-Fi is up, brain host is wrong.** This is intentional in 3A. The socket retries. Setup does not start. Opening Rocky-Setup because the Mac is asleep would drop a good LAN whenever the server stops. Any recovery (a long opt-in timer, a USB command, a future explicit request) has to keep “brain down leaves the station up” as the default. The host is per profile, so a wrong host is fixed by updating that profile, not by forgetting the SSID.

**See saved names and brain hosts on the phone.** The portal already prints SSIDs, not hosts, and only while the phone is on the AP. Showing hosts is useful. Showing passwords or `ROBOT_TOKEN` is not. There is no page on the LAN.

**Change a password or a brain host.** The same SSID already updates that slot, and a failed join rolls the RAM copy back. The phone can do that only by joining Rocky-Setup, which means Rocky has left the LAN. Doing it while he stays on the LAN needs another channel.

**Delete a saved network.** `wifi forget` refuses last-ok and compacts slots, so later slot numbers change. A phone delete needs the same refusal. Deleting the only profile means an empty list and setup on the next decision. Replacing last-ok is an in-place update (join must succeed), not a delete. The open question is how someone confirms they meant to drop a non-last-ok network, and how the UI names slots once they have been compacted.

**Intentional AP/STA switching.** 3A changes mode with the driver left running, specifically to avoid 12308. AP+STA would let the phone stay associated during a station attempt, at the cost of RAM, airtime, and a known flaky mode on ESP32-S3. Whoever designs this has to write down: which interface is up in each phase, whether a station attempt is allowed to drop the phone, and what happens on success (AP off, phone must join the LAN) and on failure (AP back, previous profiles unchanged).

**A saved network returns while Rocky-Setup is still up.** Not implemented and not verified. `Phase::Setup` does not retry. A scan while an iPhone is on the AP can drop that phone; the Stage 1 proof readme records that, which is why the proof never scans. Options that need a decision before any code: leave the portal up until the person submits or reboots; periodically leave the AP and try saved profiles (the phone drops every try); or a scan that is known not to kick the station. Do not describe “he reconnects on his own while the portal stays up” as behaviour Rocky has.

**Secrets.** `ROBOT_TOKEN`, `SETUP_AP_PASS`, and LAN passwords stay in `secrets.h` and in namespace `wifi`. They stay out of serial logs, HTML, git, and this document.

## Uncommitted Stage 1 proof

Left local on purpose. Not part of `93ab27f` or the Stage 3B.1 commit. Do not delete them, and do not stage them unless a later decision says the proof env belongs in git.

- `firmware/platformio.ini` — extra env `setup_ap_proof` only. The rest of the file is unchanged from what `v2-dev` already has.
- `firmware/test/setup_ap/README.md`
- `firmware/test/setup_ap/main.cpp`
- `firmware/test/setup_ap/setup_ap_secrets.h.example`

`firmware/test/setup_ap/setup_ap_secrets.h` is gitignored. Production does not build this env. Production serves the portal from `firmware/src/setup_portal.cpp` and reads `SETUP_AP_PASS` from `firmware/include/secrets.h`.

`net.cpp` has a fallback include of `../test/setup_ap/setup_ap_secrets.h` only when `SETUP_AP_PASS` is not already defined. This machine’s `secrets.h` defines it, so the production image does not compile the proof header. The proof sketch is still a different firmware image. Flashing `setup_ap_proof` replaces Rocky until `xiao_esp32s3` is flashed again. The proof does not write the `wifi` namespace; a full chip erase would.

## Safety and development rules

- Do not erase NVS as part of ordinary testing. Ordinary `pio run -t upload` keeps it. A full chip erase or a partition change wipes profiles; the next boot recopies the `secrets.h` seed only if `migrated` is gone.
- Code that touches preferences stays in namespace `wifi`. Leave `home` and `face` alone, including `wifi forget`.
- Do not commit `firmware/include/secrets.h`, `firmware/test/setup_ap/setup_ap_secrets.h`, `server/.env`, `server/personal_context.txt`, tokens, or LAN passwords. Templates (`secrets.h.example`, `.env.example`) are the files that belong in git.
- V1 (`desk-robot`) and V2 (`desk-robot-v2`) stay separate. Do not copy this portal into V1 from a V2 session, and do not bring V1’s `personal_context.txt` loader over as part of Wi-Fi work.
- Do not merge, rebase onto, or push `main`. Casey's upstream `main` stays as it is. Work on `v2-dev`.
- Changes stay small enough to test on the host, then build, then try on the robot.
- Do not flash, commit, or push unless the session asked for that explicitly.
- Before hardware: `python3 -m unittest tests.test_wifi_profiles` from `server/`, and `pio run -e xiao_esp32s3` from `firmware/`. Build the `xiao_esp32s3` env. Do not upload the proof env onto the robot that is about to be acceptance-tested.

## Next development focus

Wi-Fi work is closed. Home Assistant V2 is complete and pushed: commit `7407574` on `origin/v2-dev` (*Add Home Assistant read and light control*). The record is [HOME_ASSISTANT_V2_PLAN.md](HOME_ASSISTANT_V2_PLAN.md), and the wider status is [FUTURE_DEVELOPMENT.md](FUTURE_DEVELOPMENT.md).

The next development task is undecided. Do not start one from this note.

Awake idle personality, including Calm, Curious and Lively, was considered and is not being pursued. That decision is recorded in [FUTURE_DEVELOPMENT.md](FUTURE_DEVELOPMENT.md). Casey's "Look around" roaming idle screen already runs while Rocky is asleep. It is not a starting point for new awake movement.

Known Wi-Fi limit to leave alone unless it becomes a problem: Rocky does not automatically retry saved networks once the setup portal is already active.
