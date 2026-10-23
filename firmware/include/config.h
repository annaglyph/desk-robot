#pragma once

// ─── Pin map (XIAO ESP32S3 Sense) ────────────────────────────────────────────
// Silkscreen label → GPIO number. The camera, PDM mic, and SD slot on the
// Sense expansion board use their own internal pins and don't appear here.
//
//   D0  = GPIO1   I2S BCLK  → MAX98357A BCLK      (speaker)
//   D1  = GPIO2   I2S LRC   → MAX98357A LRC       (speaker)
//   D2  = GPIO3   I2S DIN   → MAX98357A DIN       (speaker)
//   D3  = GPIO4   pan servo signal (orange wire)
//   D4  = GPIO5   I2C SDA   → OLED SDA
//   D5  = GPIO6   I2C SCL   → OLED SCL
//   D6  = GPIO43  tilt servo signal (orange wire)
//   D7  = GPIO44  spare

constexpr uint8_t PIN_SERVO_PAN = 4;   // D3
constexpr uint8_t PIN_SERVO_TILT = 43; // D6

constexpr uint8_t PIN_I2S_BCLK = 1; // D0 → MAX98357A BCLK
constexpr uint8_t PIN_I2S_LRC = 2;  // D1 → MAX98357A LRC
constexpr uint8_t PIN_I2S_DIN = 3;  // D2 → MAX98357A DIN

// I2C uses the XIAO's default Wire pins (SDA=GPIO5/D4, SCL=GPIO6/D5).
// Most SH1106 modules answer at address 0x3C; U8g2 finds it automatically.

// ─── Motion limits ───────────────────────────────────────────────────────────
constexpr float PAN_MIN_DEG = -70.0f; // head turn limit, left (pulse ~2288 us)
constexpr float PAN_MAX_DEG = 70.0f;  // head turn limit, right (pulse ~811 us)
constexpr float PAN_MAX_SPEED = 180.0f; // deg/sec ceiling — keeps motion gentle
constexpr float PAN_TRIM_DEG = 9.47f;   // 1550 us horn neutral → head angle 0
constexpr bool PAN_INVERT = true;       // a longer pulse turns this head left

// Custom pan/tilt. Eye level is 0. The head can look up to +37 and down to -75.
// Pulses at these limits stay inside the measured safe windows:
// pan 800–2300 us, tilt 700–1900 us.
constexpr float TILT_MIN_DEG = -75.0f;  // look-down limit (pulse ~708 us)
constexpr float TILT_MAX_DEG = 37.0f;   // look-up limit (pulse ~1890 us)
constexpr float TILT_MAX_SPEED = 120.0f;
constexpr float TILT_TRIM_DEG = 4.74f;  // 1500 us horn neutral → head angle 0
constexpr bool TILT_INVERT = false;     // a longer pulse tilts this head up

// Watch the mechanism the first time tilt moves: if the bracket strains at
// either end of travel, pull TILT_MIN/MAX in until it stops.

// Detach the servo after it has been at rest this long. A detached servo
// doesn't buzz or burn power; the head is light enough to hold position.
constexpr uint32_t SERVO_RELAX_MS = 1500;

// ─── Microphone ──────────────────────────────────────────────────────────────
// The Sense board's PDM mic is hard-wired to these GPIOs (no header pins).
constexpr uint8_t PIN_PDM_CLK = 42;
constexpr uint8_t PIN_PDM_DATA = 41;
// Software gain on the mic samples. The PDM mic is quiet; raise if the brain
// says "too quiet", lower if loud speech clips.
constexpr float MIC_GAIN = 6.0f;

// Diagnostic switch. 0 skips PDM init, the capture task, and microphone
// uplink. Incoming speaker audio, the OLED, the servos, Wi-Fi, and the
// WebSocket stay up. The active value is -DMIC_ENABLED in platformio.ini.
#ifndef MIC_ENABLED
#define MIC_ENABLED 1
#endif

// 1 prints a 2 s timing summary and a line when one loop exceeds 50 ms.
// Stages are the WebSocket read, microphone uplink, speaker-feed copy, and
// the face/servo update. 0 leaves the loop silent.
#ifndef LOOP_TIMING
#define LOOP_TIMING 0
#endif

// ─── Camera ──────────────────────────────────────────────────────────────────
// Ribbon-up on the head. The OV2640 starts with neither flip set. Enabling
// both rotates that image 180 degrees, and on this mount the result was
// upside down, so both stay off. Enabling only one would mirror it.
constexpr bool CAMERA_VFLIP = false;
constexpr bool CAMERA_HMIRROR = false;

// Diagnostic switch. 0 skips sensor init, the capture task, and JPEG
// transmission so command latency and audio can be compared with the camera
// off the bus. Wi-Fi, the WebSocket, the mic, the OLED, the speaker, and the
// servos stay as they are. The active value is the -DCAMERA_ENABLED flag in
// platformio.ini; this default applies only when that flag is absent.
#ifndef CAMERA_ENABLED
#define CAMERA_ENABLED 1
#endif

// ─── Speaker ─────────────────────────────────────────────────────────────────
// 0.0-1.0 software volume for the MAX98357A. For more than 1.0 can give,
// tie the amp's GAIN pin to GND (15 dB instead of the floating 9 dB).
constexpr float SPEAKER_VOLUME = 0.7f;

// ─── Behavior ────────────────────────────────────────────────────────────────
// Face steps. Blinks, eye morphs and Z's advance by a fixed amount each call,
// so this stays at 33 ms when the servo tick changes.
constexpr uint32_t FRAME_INTERVAL_MS = 33; // ~30 Hz
// Servo command rate. 20 ms is one 50 Hz pulse period, and it is the motion
// laboratory's tick. Idle curves are timed in milliseconds. The manual-move
// fraction in ServoNeck scales with this value, so 33 restores the old ease.
constexpr uint32_t SERVO_INTERVAL_MS = 20;
// The OLED transfer blocks for ~30 ms, so it runs on its own slower timer.
// 50 ms is 20 fps, inside the 15–20 fps diagnostic limit.
constexpr uint32_t OLED_INTERVAL_MS = 50;
