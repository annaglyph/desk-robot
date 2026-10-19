# Two-axis motion laboratory

A bench program for Rocky's pan and tilt servos. It keeps the original
pulse calibrator and adds independent, interruptible motion on both axes
so easing and timing can be tried on the spare ESP32 before anything in
Rocky's firmware changes.

This project does not build Rocky's firmware. `firmware/` and `server/`
are not on its include path.

## Board

The diymore board with a CH340C USB chip is an ESP32-WROOM-32 (the original
ESP32), not an ESP32-C3 and not the XIAO's ESP32-S3. PlatformIO's `esp32dev`
target is that module: 4 MB flash, DIO. The environment name is
`servo-calibrator`.

Both signal pins are on the header next to VIN and GND. Counting up from
the 5V pin that header is 5V, GND, D13, D12, D14, D27, D26, D25. Silkscreen
`25` is GPIO25 and `26` is GPIO26. Both are ordinary outputs, neither is a
boot strap, and both are in ESP32Servo's allowed PWM list for this chip.
The library drives them with LEDC on the original ESP32 (MCPWM is only
compiled for the S3). `setup()` reserves LEDC timers 0 and 1, which is what
ESP32Servo expects for two servos at the same 50 Hz.

GPIO34, GPIO35, GPIO36 (VP) and GPIO39 (VN) are input-only and cannot drive
a servo. GPIO12 is a boot strap: if it is high at reset the module selects
the wrong flash voltage and may not boot. The CH340C uses GPIO1 (TX) and
GPIO3 (RX).

Pinned in `platformio.ini`:

- `espressif32@7.1.3` (Arduino-ESP32 2.0.17)
- `madhephaestus/ESP32Servo@3.2.1` at 50 Hz

Rocky's firmware asks for `ESP32Servo@^3.0.5`. 3.2.1 is the release that
satisfies that range, pinned here so this tool does not float.

## Power and wiring

The ESP32 is powered from USB. Both servos are powered from one external
regulated 5 V supply. Do not power an MG90S from the ESP32 3V3 pin, and do
not take servo power from the board's 5V or VIN pin. That pin is USB 5 V.
Two MG90S stalling together can draw about 1.2–2 A; use a supply that can
deliver 2 A. A stall on USB 5 V can reset the chip.

| Wire | Connect to |
| --- | --- |
| Pan signal (orange or yellow) | GPIO25 |
| Tilt signal (orange or yellow) | GPIO26 |
| Both servo grounds (brown or black) | External supply ground, and ESP32 GND |
| Both servo power leads (red) | External regulated 5 V only |

The grounds must meet. Without a common ground the pulses have no
reference and the servos twitch or sit humming. If the 5 V leads are long,
put 470–1000 µF across the servo power rails, close to the servos, striped
leg to ground.

Boot drives both axes to their fitted neutrals (pan 1550 µs, tilt 1500 µs)
and leaves PWM on. It does not run the demo or idle. `off` drops both
signals. Nothing in this program sweeps or searches for a mechanical stop.

## Calibrated axes

Head coordinates, matching production: pan negative is Rocky's left, pan
positive is Rocky's right, tilt negative is down, tilt positive is up, and
0° is neutral.

| | Pan | Tilt |
| --- | --- | --- |
| GPIO | 25 | 26 |
| Fitted neutral | 1550 µs | 1500 µs |
| Safe pulse window | 800–2300 µs | 700–1900 µs |
| Longer pulse | Rocky's left | up |
| Trim | +9.47° | +4.74° |
| Invert | true | false |
| Command limits | −70° to +70° | −75° to +37° |
| Demo moves | about ±15° | about −8° |

Angle commands use the production map. Pulse commands do not: they write
the microsecond value you type, inside that axis's window only.

```text
servoDeg = (invert ? -headDeg : headDeg) + trimDeg
pulse    = 500 + (int)((servoDeg + 90) / 180 * 1900)
```

`pan 0` therefore requests 1549 µs, one microsecond under the horn neutral,
because the firmware truncates. `c` and boot use the fitted 1550 µs instead.
Tilt 0° is 1500 µs either way. The integer pulse is what gets sent;
`status` reports that requested width.

## PWM resolution

ESP32Servo 3.2.1 attaches at 10 bits. On this chip that is one step every
19.53125 µs, about 1.85°. A 15° pan is only eight steps, so a 2400 ms move
jumps about every 300 ms. The library's `setTimerWidth(16)` is the supported
way to change it. At 16 bits and 50 Hz the step is 20000/65536 µs, about
0.305 µs, and the 80 MHz LEDC clock divides to 50 Hz exactly.

`attach()` always puts 10 bits back, including after `off`. Both servos
share one LEDC timer, so the second attach would also knock the first
servo back to 10 bits. Every successful attach of the pair therefore does
three things, in this order: attach both, call `setTimerWidth(16)` on
both, then write both current pulses. Writing happens after both widths
are set, because `setTimerWidth` does not rescale its stored tick count
by a power of two. `on` while the pair is already at 16 bits only
rewrites the pulses; it does not detach. `status` prints the width each
servo object is using.

The command limits sit close to the measured pulse ends. Demo stays near
neutral, about ±15° pan and −8° tilt. Idle gestures are larger — roughly
±40° pan and −30° to +12° tilt before the amplitude scale — and every
idle target is kept inside the command limits. A typed angle or pulse
outside its own limits is rejected and the axis is left where it was.
Nothing is clamped to the limit in place of the command.

## Motion

Pan and tilt each interpolate on their own, from the angle they are
commanding now to the angle you asked for. The loop updates both about
every 20 ms from the elapsed time, so serial input stays live and a new
command replaces the in-progress move without returning to an old start.
A delayed move samples its start angle when the delay ends, not when you
typed the command. Servos stay attached for the whole session until `off`.

Three profiles, chosen per move:

- `linear` — constant speed
- `cosine` — ease in and out, `(1 - cos(πt)) / 2`
- `smootherstep` — `6t⁵ − 15t⁴ + 10t³`

The move ends on the exact integer pulse for the destination.

**Duration** is how long that move should take, in milliseconds. `pan 15`
uses the current speed preset. `move` and `look` take a duration on the
command. Presets only change that default:

| Preset | Default duration | What to listen for |
| --- | --- | --- |
| `quick` | 350 ms | attention |
| `normal` | 800 ms | a deliberate turn |
| `slow` | 1600 ms | curious |
| `subtle` | 2400 ms | a small listening gesture |

**Ceiling** is a maximum angular speed in degrees per second. It never
shortens a move. If the requested duration would be faster than the
ceiling, the duration is stretched to `|delta| / ceiling`. A 2° nudge with
`subtle` is already slow, so the ceiling does nothing. A `quick` move
across 40° with `ceiling 30` is lengthened. `ceiling off` is the default.
Demo steps ignore the ceiling so the three eases share a 1000 ms duration.
Idle and typed moves honor it.

`look` starts pan immediately and can start tilt later with `after`.
`move … after` delays that one axis. Different durations and eases are
just two `move` lines. `stop`, `off`, and any motion command cancel a
delayed start as well as a move that has already begun.

## Demo and idle

`demo` is manual. It runs five comparisons, each out and back to neutral,
with about 1.5 s of stillness between moves. Steps 1–3 pan to +15° and
home, in linear, cosine, then smootherstep, 1000 ms, so the profile is the
only difference. Step 4 pans to +15° and tilts to −8° together, cosine.
Step 5 does the same pair with tilt starting 150 ms later, smootherstep.
Any motion command, `stop`, or `off` abandons the rest.

`idle` is off until you start it. It does not wander a few degrees. It
looks at one place, holds, then looks somewhere else, using the same
eased moves as `look`.

About 70% of gestures are one of three glances: curious (pan about
±20–35°, tilt −5° to −15°, cosine, 700–1100 ms), thoughtful (pan about
±10–25°, tilt −15° to −25°, smootherstep, 1000–1500 ms, then a longer
hold), or attentive (pan about ±15–30°, tilt −5° to +10°, cosine,
600–1000 ms). About 20% are smaller adjustments, and about 10% are a
wider exploratory look. Some moves are pan only, some tilt only, and
some move both, with tilt occasionally starting 100–200 ms later. Pan
does not repeat the same direction. The head does not come home after
every glance; a return shows up after a couple of looks, when it is
already away from centre. Holds are usually 2–6 seconds, sometimes
longer. There is no overshoot and no oscillation.

`idle curious` is the default shape. `idle calm` is smaller, slower, and
less frequent. `idle lively` is a little larger and quicker. Naming a
preset starts idle, or changes the next gesture if idle is already
running. These three knobs override the preset and mark it `custom`:

```text
idle scale 0.8          amplitude, 0.3–1.5, 1 = curious
idle duration 1.3       multiplies gesture time, 0.4–2
idle pause 3000 7000    hold at each pose, milliseconds
```

`idle on` uses whatever is currently set. `idle off`, a manual move,
`stop`, and `off` all cancel it. `stop` holds the angles it has reached.
Each gesture prints one line, for example
`[idle] curious glance: pan +28°, tilt -12°, 900 ms, tilt +150 ms`.

## Console

115200 baud. The loop never blocks on a move. The PlatformIO monitor does
not echo keystrokes, so the board does. Backspace works. Return ends the
line.

Bare numbers are **microseconds**, not degrees, and they apply to the
selected axis (`axis pan` or `axis tilt`, pan at boot). `us` names the
axis so a pulse cannot be read as an angle. `pan` and `tilt` followed by
a small number are degrees.

| Command | Effect |
| --- | --- |
| `axis pan` / `axis tilt` | Select the axis for bare pulse commands |
| `1550` | Snap the selected axis to that pulse |
| `+` / `-` / `++` / `--` | ±10 µs or ±50 µs on the selected axis |
| `c` | Snap the selected axis to its fitted neutral pulse |
| `us pan 1550` | Pulse on a named axis. Also `us tilt +` |
| `pan 15` | Ease pan to +15° with the default duration and profile |
| `tilt -10` | Ease tilt to −10° |
| `move pan 20 800 cosine` | One axis, duration, profile |
| `move tilt -10 1200 smootherstep after 150` | Same, starting 150 ms later |
| `look 15 -8 900 cosine` | Both axes, same duration and profile, together |
| `look 15 -8 900 cosine after 150` | Tilt starts 150 ms after pan |
| `ease linear\|cosine\|smootherstep` | Default profile |
| `speed quick\|normal\|slow\|subtle` | Default duration |
| `ceiling 30` / `ceiling off` | Optional speed cap, or none |
| `center` | Both axes ease to 0° |
| `demo` | The five comparisons |
| `idle on` / `idle off` | Start idle with the current knobs, or stop it |
| `idle calm` / `curious` / `lively` | Preset amplitude, timing, and holds |
| `idle scale 0.8` | Amplitude. 1 matches the curious ranges |
| `idle duration 1.3` | Stretch or shorten gesture time |
| `idle pause 3000 7000` | Minimum and maximum hold, in milliseconds |
| `stop` | Cancel scheduled motion and hold the current angles |
| `off` | Cancel everything and hold both signal pins low |
| `on` | Attach both servos again at the held pulses |
| `status` | Mode, pulses, targets, time left, PWM, timer bits |
| `help` | This list |

`status` prints the mode (`manual`, `demo`, or `idle`), PWM, the timer
width and 50 Hz frame, the speed preset, the default profile, the ceiling,
the idle preset with its scale, duration, and hold range, the selected
pulse axis, and for each axis the commanded angle, the requested pulse,
and either `holding`, the remaining time, or the delay before a scheduled
move. A move prints one line when it is accepted and one line when it
arrives. Idle adds one line when a gesture starts. The 50 Hz updates are
not printed.

Angles outside the command limits, and pulses outside that axis's window,
are rejected. So is a duration outside 1–30000 ms.

## Why `off` stops the pulses

`Servo.detach()` on this chip only disconnects the pad from LEDC
(`ledcDetachPin` is `pinMatrixOutDetach` in Arduino-ESP32 2.0.17). The
timer can keep running and a floating servo line can twitch. `off`
detaches both library objects, calls `ledcDetachPin` on GPIO25 and GPIO26,
then `gpio_reset_pin` and drives each pin low as a plain output. A steady
low is not a servo pulse, so an MG90S drops holding torque.

Gear friction can still hold a light horn. `gpio_reset_pin` enables the
pad pull-up for a few instructions before the pin is driven low. That blip
is not a 1–2 ms pulse.

## Build

From this directory, with PlatformIO on PATH or via
`~/.platformio/penv/bin/pio`:

```bash
pio run -e servo-calibrator
```

Upload is a separate step. Do not upload until the ESP32 is plugged in and
you have asked for it.

```bash
pio run -e servo-calibrator -t upload
pio device monitor -e servo-calibrator
```

On macOS the CH340C port is usually `/dev/cu.wchusbserial…` or
`/dev/cu.usbserial-…`. If upload cannot find the port, hold BOOT, tap EN,
release BOOT, and try again. Opening the monitor resets the board, which
drives both servos to neutral with PWM on.
