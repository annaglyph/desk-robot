// Two-axis motion laboratory for Rocky's pan/tilt servos.
// Pulse calibration and angle motion share one ESP32-WROOM-32.
// Rocky's production firmware is not part of this build.
//
// Pan  GPIO25   neutral 1550 us   safe 800-2300 us   invert   trim +9.47
// Tilt GPIO26   neutral 1500 us   safe 700-1900 us   no invert trim +4.74
// Head angle 0 maps through the same formula as firmware/src/servo_neck.cpp.

#include <Arduino.h>
#include <ESP32Servo.h>
#include <driver/gpio.h>
#include <esp_arduino_version.h>

#include <math.h>
#include <stdlib.h>
#include <string.h>

static constexpr int kFrequencyHz = 50;
static constexpr int kTimerBits = 16;
static constexpr uint32_t kTickMs = 20;

enum class Ease { Linear, Cosine, Smooth };
enum class Mode { Manual, Demo, Idle };

struct Axis {
  const char* name;
  int pin;
  int minUs;
  int maxUs;
  float minDeg;
  float maxDeg;
  float trim;
  bool invert;
  float autoMinDeg;
  float autoMaxDeg;

  Servo servo;
  float angle;
  int pulse;
  bool moving;
  bool pending;
  uint32_t startMs;
  uint32_t durMs;
  uint32_t pendingAt;
  float fromDeg;
  float toDeg;
  Ease ease;
  bool doneNoted;

  Axis(const char* name, int pin, int minUs, int maxUs, float minDeg, float maxDeg, float trim,
       bool invert, float autoMinDeg, float autoMaxDeg)
      : name(name),
        pin(pin),
        minUs(minUs),
        maxUs(maxUs),
        minDeg(minDeg),
        maxDeg(maxDeg),
        trim(trim),
        invert(invert),
        autoMinDeg(autoMinDeg),
        autoMaxDeg(autoMaxDeg),
        angle(0),
        pulse(1500),
        moving(false),
        pending(false),
        startMs(0),
        durMs(0),
        pendingAt(0),
        fromDeg(0),
        toDeg(0),
        ease(Ease::Cosine),
        doneNoted(true) {}
};

static Axis pan("pan", 25, 800, 2300, -70.0f, 70.0f, 9.47f, true, -15.0f, 15.0f);
static Axis tilt("tilt", 26, 700, 1900, -75.0f, 37.0f, 4.74f, false, -8.0f, 8.0f);

static bool pwmOn = false;
static Mode mode = Mode::Manual;
static Axis* selected = &pan;
static Ease defaultEase = Ease::Cosine;
static uint32_t defaultDurMs = 800;
static float ceilingDegPerSec = 0;  // 0 = duration alone sets the timing
static const char* speedName = "normal";

static int lastPanSign = 0;
static uint32_t nextIdleMs = 0;
static const char* idlePresetName = "curious";
static float idleAmp = 1.0f;
static float idleDurScale = 1.0f;
static uint32_t idlePauseMin = 2000;
static uint32_t idlePauseMax = 6000;
static int idleLongPct = 12;
static uint32_t idleHoldBonus = 0;
static int gesturesSinceHome = 0;
static bool lastIdleWasHome = false;

static int demoStep = 0;
static bool demoWaitMove = false;
static uint32_t demoPauseUntil = 0;

static char line[96];
static size_t lineLen = 0;
static bool lineOverflow = false;
static uint32_t lastTickMs = 0;

static const char* easeName(Ease ease) {
  switch (ease) {
    case Ease::Linear: return "linear";
    case Ease::Cosine: return "cosine";
    case Ease::Smooth: return "smootherstep";
  }
  return "?";
}

static bool parseEase(const char* text, Ease* ease) {
  if (strcasecmp(text, "linear") == 0) *ease = Ease::Linear;
  else if (strcasecmp(text, "cosine") == 0) *ease = Ease::Cosine;
  else if (strcasecmp(text, "smootherstep") == 0 || strcasecmp(text, "smooth") == 0)
    *ease = Ease::Smooth;
  else
    return false;
  return true;
}

static float easeValue(Ease ease, float t) {
  if (t <= 0) return 0;
  if (t >= 1) return 1;
  switch (ease) {
    case Ease::Linear: return t;
    case Ease::Cosine: return (1.0f - cosf(PI * t)) * 0.5f;
    case Ease::Smooth: {
      // 6t^5 - 15t^4 + 10t^3
      float t2 = t * t;
      float t3 = t2 * t;
      return t3 * (t * (t * 6.0f - 15.0f) + 10.0f);
    }
  }
  return t;
}

// Same map as ServoNeck::writeAngle. Integer truncation matches the firmware.
static int pulseForAngle(const Axis& axis, float deg) {
  float servoDeg = (axis.invert ? -deg : deg) + axis.trim;
  float t = (servoDeg + 90.0f) / 180.0f;
  return 500 + static_cast<int>(t * 1900.0f);
}

static float angleForPulse(const Axis& axis, int us) {
  float servoDeg = (static_cast<float>(us) - 500.0f) / 1900.0f * 180.0f - 90.0f;
  return axis.invert ? (axis.trim - servoDeg) : (servoDeg - axis.trim);
}

static void holdPinLow(int pin) {
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
  ledcDetach(pin);
#else
  ledcDetachPin(pin);
#endif
  gpio_reset_pin(static_cast<gpio_num_t>(pin));
  gpio_set_direction(static_cast<gpio_num_t>(pin), GPIO_MODE_OUTPUT);
  gpio_set_level(static_cast<gpio_num_t>(pin), 0);
}

static bool attachAxis(Axis& axis) {
  if (axis.servo.attached()) return true;
  // setPeriodHertz only stores 50 Hz. attach() then forces the library
  // default of 10 bits; the shared LEDC timer is raised to 16 afterwards.
  axis.servo.setPeriodHertz(kFrequencyHz);
  axis.servo.attach(axis.pin, axis.minUs, axis.maxUs);
  if (!axis.servo.attached()) {
    holdPinLow(axis.pin);
    Serial.printf("attach failed on %s GPIO%d\n", axis.name, axis.pin);
    return false;
  }
  return true;
}

static void writePulse(Axis& axis) {
  int us = axis.pulse;
  if (us < axis.minUs) us = axis.minUs;
  if (us > axis.maxUs) us = axis.maxUs;
  axis.servo.writeMicroseconds(us);
}

static bool pwmEnable();

static void writeAxis(Axis& axis) {
  if (!pwmOn) return;
  // One axis attaching at the library default would program the shared
  // timer back to 10 bits. Any gap goes through both servos together.
  if (!pan.servo.attached() || !tilt.servo.attached() ||
      pan.servo.readTimerWidth() != kTimerBits || tilt.servo.readTimerWidth() != kTimerBits) {
    pwmEnable();
    return;
  }
  writePulse(axis);
}

static void freezeAxis(Axis& axis) {
  axis.moving = false;
  axis.pending = false;
  axis.doneNoted = true;
}

static void stopMotion();

static void cancelAuto() {
  if (mode == Mode::Manual) return;
  Serial.printf("%s cancelled\n", mode == Mode::Demo ? "demo" : "idle");
  mode = Mode::Manual;
  demoStep = 0;
  demoWaitMove = false;
  stopMotion();
}

static bool pulseAllowed(const Axis& axis, long us) {
  return us >= axis.minUs && us <= axis.maxUs;
}

static void snapPulse(Axis& axis, int us) {
  freezeAxis(axis);
  axis.pulse = us;
  axis.angle = angleForPulse(axis, us);
  writeAxis(axis);
}

static uint32_t durationFor(float fromDeg, float toDeg, uint32_t requested, bool honorCeiling) {
  if (!honorCeiling || ceilingDegPerSec <= 0) return requested;
  float delta = fabsf(toDeg - fromDeg);
  uint32_t floorMs = static_cast<uint32_t>((delta / ceilingDegPerSec) * 1000.0f + 0.5f);
  return floorMs > requested ? floorMs : requested;
}

// Start or schedule a move. The from-angle is the live commanded angle, so a
// new command interrupts without jumping back to an old start.
static bool angleAccepted(const Axis& axis, float targetDeg) {
  if (targetDeg < axis.minDeg || targetDeg > axis.maxDeg) {
    Serial.printf("rejected: %s %.1f deg is outside %.0f..%.0f\n", axis.name, targetDeg,
                  axis.minDeg, axis.maxDeg);
    return false;
  }
  int us = pulseForAngle(axis, targetDeg);
  if (us < axis.minUs || us > axis.maxUs) {
    Serial.printf("rejected: %s %.1f deg maps to %d us, outside %d-%d\n", axis.name, targetDeg,
                  us, axis.minUs, axis.maxUs);
    return false;
  }
  return true;
}

static bool startMove(Axis& axis, float targetDeg, uint32_t requestedMs, Ease ease,
                      uint32_t delayMs, bool honorCeiling, bool announce) {
  if (targetDeg < axis.minDeg || targetDeg > axis.maxDeg) {
    Serial.printf("rejected: %s %.1f deg is outside %.0f..%.0f\n", axis.name, targetDeg,
                  axis.minDeg, axis.maxDeg);
    return false;
  }
  int us = pulseForAngle(axis, targetDeg);
  if (!pulseAllowed(axis, us)) {
    Serial.printf("rejected: %s %.1f deg maps to %d us, outside %d-%d\n", axis.name,
                  targetDeg, us, axis.minUs, axis.maxUs);
    return false;
  }
  if (requestedMs == 0 || requestedMs > 30000) {
    Serial.println(F("rejected: duration must be 1-30000 ms"));
    return false;
  }
  uint32_t dur = durationFor(axis.angle, targetDeg, requestedMs, honorCeiling);
  axis.toDeg = targetDeg;
  axis.ease = ease;
  axis.durMs = dur;
  axis.doneNoted = false;
  axis.pending = delayMs > 0;
  if (axis.pending) {
    axis.moving = false;
    axis.pendingAt = millis() + delayMs;
  } else {
    axis.pending = false;
    axis.moving = true;
    axis.fromDeg = axis.angle;
    axis.startMs = millis();
  }
  if (!pwmOn) pwmEnable();
  if (announce) {
    if (dur != requestedMs) {
      Serial.printf("%s %.1f deg in %lu ms (%s, ceiling %.0f deg/s lengthened %lu ms)\n",
                    axis.name, targetDeg, static_cast<unsigned long>(dur), easeName(ease),
                    ceilingDegPerSec, static_cast<unsigned long>(dur - requestedMs));
    } else if (delayMs > 0) {
      Serial.printf("%s %.1f deg in %lu ms %s, starts in %lu ms\n", axis.name, targetDeg,
                    static_cast<unsigned long>(dur), easeName(ease),
                    static_cast<unsigned long>(delayMs));
    } else {
      Serial.printf("%s %.1f deg in %lu ms %s\n", axis.name, targetDeg,
                    static_cast<unsigned long>(dur), easeName(ease));
    }
  }
  return true;
}

static void updateAxis(Axis& axis, uint32_t now) {
  if (axis.pending) {
    if (static_cast<int32_t>(now - axis.pendingAt) < 0) return;
    axis.pending = false;
    axis.moving = true;
    axis.fromDeg = axis.angle;
    axis.startMs = now;
    axis.doneNoted = false;
  }
  if (!axis.moving) return;
  uint32_t elapsed = now - axis.startMs;
  float t = axis.durMs == 0 ? 1.0f : static_cast<float>(elapsed) / static_cast<float>(axis.durMs);
  if (t >= 1.0f) {
    axis.angle = axis.toDeg;
    axis.moving = false;
    axis.pulse = pulseForAngle(axis, axis.angle);
    writeAxis(axis);
    if (!axis.doneNoted) {
      axis.doneNoted = true;
      Serial.printf("%s arrived %.1f deg, %d us\n", axis.name, axis.angle, axis.pulse);
    }
    return;
  }
  axis.angle = axis.fromDeg + (axis.toDeg - axis.fromDeg) * easeValue(axis.ease, t);
  axis.pulse = pulseForAngle(axis, axis.angle);
  writeAxis(axis);
}

static void stopMotion() {
  freezeAxis(pan);
  freezeAxis(tilt);
  writeAxis(pan);
  writeAxis(tilt);
}

static void pwmOff() {
  cancelAuto();
  stopMotion();
  if (pan.servo.attached()) pan.servo.detach();
  if (tilt.servo.attached()) tilt.servo.detach();
  holdPinLow(pan.pin);
  holdPinLow(tilt.pin);
  pwmOn = false;
  Serial.printf("pwm off, GPIO%d and GPIO%d held low\n", pan.pin, tilt.pin);
}

static bool pwmEnable() {
  // Sample this before attach(). A fresh attach() resets each object's
  // width to 10, and the second attach() reprograms the one shared timer.
  bool needWidth = !pan.servo.attached() || !tilt.servo.attached() ||
                   pan.servo.readTimerWidth() != kTimerBits ||
                   tilt.servo.readTimerWidth() != kTimerBits;
  pwmOn = true;
  if (!attachAxis(pan) || !attachAxis(tilt)) {
    if (pan.servo.attached()) pan.servo.detach();
    if (tilt.servo.attached()) tilt.servo.detach();
    holdPinLow(pan.pin);
    holdPinLow(tilt.pin);
    pwmOn = false;
    Serial.println(F("attach failed, both pins held low"));
    return false;
  }
  if (needWidth) {
    // Set both widths before writing either pulse. setTimerWidth detaches
    // and reattaches that pin, and its stored-tick rescale is not a power
    // of two, so the following writes are what actually reach the pins.
    pan.servo.setTimerWidth(kTimerBits);
    tilt.servo.setTimerWidth(kTimerBits);
  }
  writePulse(pan);
  writePulse(tilt);
  return true;
}

static const char* modeName() {
  switch (mode) {
    case Mode::Manual: return "manual";
    case Mode::Demo: return "demo";
    case Mode::Idle: return "idle";
  }
  return "?";
}

static void printAxisStatus(const Axis& axis, uint32_t now) {
  Serial.printf("%-4s  %.1f deg  pulse %d us", axis.name, axis.angle, axis.pulse);
  if (axis.pending) {
    uint32_t wait = static_cast<int32_t>(axis.pendingAt - now) > 0 ? axis.pendingAt - now : 0;
    Serial.printf("  waiting %lu ms then -> %.1f deg %s %lu ms\n",
                  static_cast<unsigned long>(wait), axis.toDeg, easeName(axis.ease),
                  static_cast<unsigned long>(axis.durMs));
  } else if (axis.moving) {
    uint32_t left = axis.startMs + axis.durMs > now ? axis.startMs + axis.durMs - now : 0;
    Serial.printf("  -> %.1f deg %s  %lu ms left\n", axis.toDeg, easeName(axis.ease),
                  static_cast<unsigned long>(left));
  } else {
    Serial.println(F("  holding"));
  }
}

static void printStatus() {
  uint32_t now = millis();
  Serial.printf("mode      %s\n", modeName());
  Serial.printf("pwm       %s\n", pwmOn ? "on" : "off");
  int panBits = pan.servo.readTimerWidth();
  int tiltBits = tilt.servo.readTimerWidth();
  if (panBits == tiltBits) {
    Serial.printf("timer     %d-bit, %d Hz\n", panBits, kFrequencyHz);
  } else {
    Serial.printf("timer     pan %d-bit, tilt %d-bit, %d Hz\n", panBits, tiltBits, kFrequencyHz);
  }
  if (ceilingDegPerSec > 0) {
    Serial.printf("speed     %s (%lu ms default)  ease %s  ceiling %.0f deg/s\n", speedName,
                  static_cast<unsigned long>(defaultDurMs), easeName(defaultEase),
                  ceilingDegPerSec);
  } else {
    Serial.printf("speed     %s (%lu ms default)  ease %s  ceiling off\n", speedName,
                  static_cast<unsigned long>(defaultDurMs), easeName(defaultEase));
  }
  Serial.printf("selected  %s (bare pulse commands)\n", selected->name);
  printAxisStatus(pan, now);
  printAxisStatus(tilt, now);
  Serial.printf("limits    pan %d-%d us, %.0f..%.0f deg, GPIO%d\n", pan.minUs, pan.maxUs,
                pan.minDeg, pan.maxDeg, pan.pin);
  Serial.printf("          tilt %d-%d us, %.0f..%.0f deg, GPIO%d\n", tilt.minUs, tilt.maxUs,
                tilt.minDeg, tilt.maxDeg, tilt.pin);
  Serial.printf("idle      %s  scale %.2f  duration %.2f  hold %lu-%lu ms\n", idlePresetName,
                idleAmp, idleDurScale, static_cast<unsigned long>(idlePauseMin),
                static_cast<unsigned long>(idlePauseMax));
}

static void printHelp() {
  Serial.println(F("motion lab — pan GPIO25, tilt GPIO26, 50 Hz, 16-bit timer"));
  Serial.println(F("Boot holds angle 0 (neutral). No demo or idle until you ask."));
  Serial.println(F("Angles: pan - = left, + = right. Tilt - = down, + = up."));
  Serial.println(F("Pulse calibration (selected axis, default pan; snaps, no easing):"));
  Serial.println(F("  axis pan|tilt     choose the axis for bare pulse commands"));
  Serial.println(F("  1550              absolute microseconds on the selected axis"));
  Serial.println(F("  + / -             ±10 us     ++ / --   ±50 us"));
  Serial.println(F("  c                 snap selected axis to its neutral pulse"));
  Serial.println(F("  us pan 1550       absolute microseconds, either axis"));
  Serial.println(F("  us tilt +         same nudges: + - ++ --"));
  Serial.println(F("Angle motion (eased, interruptible):"));
  Serial.println(F("  pan 15            pan to +15 deg, default duration and ease"));
  Serial.println(F("  tilt -10"));
  Serial.println(F("  move pan 20 800 cosine"));
  Serial.println(F("  move tilt -10 1200 smootherstep after 150"));
  Serial.println(F("  look 15 -8 900 cosine"));
  Serial.println(F("  look 15 -8 900 cosine after 150   tilt starts 150 ms later"));
  Serial.println(F("  ease linear|cosine|smootherstep"));
  Serial.println(F("  speed quick|normal|slow|subtle    default duration only"));
  Serial.println(F("  ceiling 30|off     max deg/s; lengthens a move, never shortens it"));
  Serial.println(F("  center            both axes to 0 deg"));
  Serial.println(F("  demo              compare eases, then together vs staggered"));
  Serial.println(F("  idle on|off"));
  Serial.println(F("  idle calm|curious|lively"));
  Serial.println(F("  idle scale 0.7        amplitude, 1 = curious"));
  Serial.println(F("  idle duration 1.2     gesture time, 1 = preset"));
  Serial.println(F("  idle pause 2000 6000  hold at each pose, ms"));
  Serial.println(F("  stop              cancel motion, hold the current angles"));
  Serial.println(F("  off / on          both PWM pins"));
  Serial.println(F("  status / help"));
  Serial.println(F("Invalid angles and pulses are rejected. Idle stays inside those limits."));
}

static int tokenize(char* text, char** tok, int maxTok) {
  int n = 0;
  char* p = text;
  while (*p != '\0' && n < maxTok) {
    while (*p == ' ' || *p == '\t') ++p;
    if (*p == '\0') break;
    tok[n++] = p;
    while (*p != '\0' && *p != ' ' && *p != '\t') ++p;
    if (*p != '\0') *p++ = '\0';
  }
  return n;
}

static bool parseLong(const char* text, long* out) {
  char* end = nullptr;
  long value = strtol(text, &end, 10);
  if (end == text || *end != '\0') return false;
  *out = value;
  return true;
}

static bool parseFloat(const char* text, float* out) {
  char* end = nullptr;
  float value = strtof(text, &end);
  if (end == text || *end != '\0') return false;
  *out = value;
  return true;
}

static Axis* axisByName(const char* name) {
  if (strcasecmp(name, "pan") == 0) return &pan;
  if (strcasecmp(name, "tilt") == 0) return &tilt;
  return nullptr;
}

static bool nudgeToken(const char* text, int* delta) {
  if (strcmp(text, "+") == 0) *delta = 10;
  else if (strcmp(text, "-") == 0) *delta = -10;
  else if (strcmp(text, "++") == 0) *delta = 50;
  else if (strcmp(text, "--") == 0) *delta = -50;
  else
    return false;
  return true;
}

static void commandPulse(Axis& axis, long us) {
  cancelAuto();
  if (!pulseAllowed(axis, us)) {
    Serial.printf("rejected: %s %ld us is outside %d-%d\n", axis.name, us, axis.minUs, axis.maxUs);
    return;
  }
  if (!pwmOn && !pwmEnable()) return;
  snapPulse(axis, static_cast<int>(us));
  Serial.printf("%s pulse %d us, pwm on\n", axis.name, axis.pulse);
}

static void beginDemo() {
  stopMotion();
  mode = Mode::Demo;
  demoStep = 0;
  demoWaitMove = false;
  demoPauseUntil = millis();
  Serial.println(F("demo started — any motion command cancels it"));
}

static void beginIdle() {
  stopMotion();
  mode = Mode::Idle;
  nextIdleMs = millis() + 1200;
  gesturesSinceHome = 0;
  lastIdleWasHome = false;
  idleHoldBonus = 0;
  Serial.printf("idle on (%s) — scale %.2f, duration %.2f, hold %lu-%lu ms\n", idlePresetName,
                idleAmp, idleDurScale, static_cast<unsigned long>(idlePauseMin),
                static_cast<unsigned long>(idlePauseMax));
}

static void applyIdlePreset(const char* name) {
  if (strcasecmp(name, "calm") == 0) {
    idleAmp = 0.70f;
    idleDurScale = 1.30f;
    idlePauseMin = 3500;
    idlePauseMax = 8000;
    idleLongPct = 25;
    idlePresetName = "calm";
  } else if (strcasecmp(name, "lively") == 0) {
    idleAmp = 1.10f;
    idleDurScale = 0.80f;
    idlePauseMin = 1200;
    idlePauseMax = 3500;
    idleLongPct = 8;
    idlePresetName = "lively";
  } else {
    idleAmp = 1.0f;
    idleDurScale = 1.0f;
    idlePauseMin = 2000;
    idlePauseMax = 6000;
    idleLongPct = 12;
    idlePresetName = "curious";
  }
}

static bool axesSettled() {
  return !pan.moving && !pan.pending && !tilt.moving && !tilt.pending;
}

static void demoStart(float panDeg, float tiltDeg, uint32_t panDelay, uint32_t tiltDelay,
                      Ease ease) {
  Serial.printf("demo: pan %.0f and tilt %.0f, %s", panDeg, tiltDeg, easeName(ease));
  if (tiltDelay > panDelay) {
    Serial.printf(", tilt %lu ms later\n", static_cast<unsigned long>(tiltDelay - panDelay));
  } else {
    Serial.println(F(", together"));
  }
  startMove(pan, panDeg, 1000, ease, panDelay, false, true);
  startMove(tilt, tiltDeg, 1000, ease, tiltDelay, false, true);
  demoWaitMove = true;
}

static void advanceDemo(uint32_t now) {
  if (demoWaitMove) {
    if (!axesSettled()) return;
    demoWaitMove = false;
    demoPauseUntil = now + 1500;
    return;
  }
  if (static_cast<int32_t>(now - demoPauseUntil) < 0) return;
  switch (demoStep) {
    case 0: Serial.println(F("demo 1/5 linear")); demoStart(15, 0, 0, 0, Ease::Linear); break;
    case 1: demoStart(0, 0, 0, 0, Ease::Linear); break;
    case 2: Serial.println(F("demo 2/5 cosine")); demoStart(15, 0, 0, 0, Ease::Cosine); break;
    case 3: demoStart(0, 0, 0, 0, Ease::Cosine); break;
    case 4: Serial.println(F("demo 3/5 smootherstep")); demoStart(15, 0, 0, 0, Ease::Smooth); break;
    case 5: demoStart(0, 0, 0, 0, Ease::Smooth); break;
    case 6: Serial.println(F("demo 4/5 together")); demoStart(15, -8, 0, 0, Ease::Cosine); break;
    case 7: demoStart(0, 0, 0, 0, Ease::Cosine); break;
    case 8: Serial.println(F("demo 5/5 staggered")); demoStart(15, -8, 0, 150, Ease::Smooth); break;
    case 9: demoStart(0, 0, 0, 0, Ease::Smooth); break;
    default:
      mode = Mode::Manual;
      Serial.println(F("demo done"));
      return;
  }
  ++demoStep;
}

static float clampDeg(const Axis& axis, float deg) {
  if (deg < axis.minDeg) return axis.minDeg;
  if (deg > axis.maxDeg) return axis.maxDeg;
  return deg;
}

static float rangePick(float lo, float hi) {
  if (hi < lo) {
    float swap = lo;
    lo = hi;
    hi = swap;
  }
  return lo + (hi - lo) * (static_cast<float>(random(0, 101)) / 100.0f);
}

static uint32_t scaleDur(uint32_t lo, uint32_t hi) {
  uint32_t base = lo + static_cast<uint32_t>(random(0, static_cast<long>(hi - lo) + 1));
  float scaled = static_cast<float>(base) * idleDurScale;
  if (scaled < 250.0f) scaled = 250.0f;
  if (scaled > 8000.0f) scaled = 8000.0f;
  return static_cast<uint32_t>(scaled + 0.5f);
}

static float aimedPan(float mag) {
  float signedMag = fabsf(mag) * idleAmp;
  int dir = lastPanSign > 0 ? -1 : (lastPanSign < 0 ? 1 : (pan.angle >= 0.0f ? -1 : 1));
  lastPanSign = dir;
  return clampDeg(pan, dir * signedMag);
}

static void noteIdleMove(const char* name, bool doPan, float panTarget, bool doTilt,
                         float tiltTarget, uint32_t dur, uint32_t stagger) {
  Serial.print(F("[idle] "));
  Serial.print(name);
  Serial.print(':');
  if (doPan) Serial.printf(" pan %+.0f°", panTarget);
  if (doTilt) Serial.printf(" tilt %+.0f°", tiltTarget);
  Serial.printf(", %lu ms", static_cast<unsigned long>(dur));
  if (stagger > 0) Serial.printf(", tilt +%lu ms", static_cast<unsigned long>(stagger));
  Serial.println();
}

static void planIdle(uint32_t now) {
  (void)now;
  bool goHome = !lastIdleWasHome && gesturesSinceHome >= 2 && random(0, 100) < 18;
  if (goHome && fabsf(pan.angle) < 3.0f && fabsf(tilt.angle) < 3.0f) goHome = false;
  if (goHome) {
    bool doPan = fabsf(pan.angle) >= 3.0f;
    bool doTilt = fabsf(tilt.angle) >= 3.0f;
    uint32_t dur = scaleDur(800, 1100);
    if (doPan) startMove(pan, 0, dur, Ease::Cosine, 0, true, false);
    if (doTilt) startMove(tilt, 0, dur, Ease::Cosine, 0, true, false);
    noteIdleMove("return", doPan, 0, doTilt, 0, dur, 0);
    lastIdleWasHome = true;
    gesturesSinceHome = 0;
    lastPanSign = 0;
    nextIdleMs = 0;
    return;
  }

  int roll = random(0, 100);
  // 70% named glances, 20% small adjustments, 10% a wider look.
  int family = roll < 70 ? 0 : (roll < 90 ? 1 : 2);
  int gesture = family == 0 ? random(0, 3) : (family == 1 ? 3 : 4);
  const char* name = "curious glance";
  float panLo = 20.0f;
  float panHi = 35.0f;
  float tiltLo = -15.0f;
  float tiltHi = -5.0f;
  uint32_t durLo = 700;
  uint32_t durHi = 1100;
  Ease ease = Ease::Cosine;
  int staggerPct = 40;
  if (gesture == 1) {
    name = "thoughtful look";
    panLo = 10.0f;
    panHi = 25.0f;
    tiltLo = -25.0f;
    tiltHi = -15.0f;
    durLo = 1000;
    durHi = 1500;
    ease = Ease::Smooth;
    staggerPct = 20;
    idleHoldBonus = 1500 + static_cast<uint32_t>(random(0, 1500));
  } else if (gesture == 2) {
    name = "attentive glance";
    panLo = 15.0f;
    panHi = 30.0f;
    tiltLo = -5.0f;
    tiltHi = 10.0f;
    durLo = 600;
    durHi = 1000;
    staggerPct = 30;
  } else if (gesture == 3) {
    name = "small adjustment";
    panLo = 6.0f;
    panHi = 14.0f;
    tiltLo = -6.0f;
    tiltHi = 6.0f;
    durLo = 500;
    durHi = 800;
    staggerPct = 20;
  } else if (gesture == 4) {
    name = "exploratory look";
    panLo = 30.0f;
    panHi = 42.0f;
    tiltLo = -28.0f;
    tiltHi = 12.0f;
    durLo = 1100;
    durHi = 1600;
    ease = Ease::Smooth;
    staggerPct = 35;
  }

  int axes = random(0, 100);
  bool doPan = true;
  bool doTilt = true;
  if (family == 1) {
    if (axes < 40) doTilt = false;
    else if (axes < 80) doPan = false;
  } else if (family == 2) {
    if (axes < 20) doTilt = false;
    else if (axes < 30) doPan = false;
  } else if (axes < 25) {
    doTilt = false;
  } else if (axes < 40) {
    doPan = false;
  }

  float panTarget = 0;
  float tiltTarget = 0;
  if (doPan) {
    panTarget = aimedPan(rangePick(panLo, panHi));
    if (fabsf(panTarget - pan.angle) < 6.0f) {
      float pushed = fabsf(panHi) * idleAmp;
      if (pushed < 12.0f) pushed = 12.0f;
      panTarget = clampDeg(pan, static_cast<float>(lastPanSign) * pushed);
    }
  }
  if (doTilt) tiltTarget = clampDeg(tilt, rangePick(tiltLo, tiltHi) * idleAmp);
  if (doPan && fabsf(panTarget - pan.angle) < 3.0f) doPan = false;
  if (doTilt && fabsf(tiltTarget - tilt.angle) < 3.0f) doTilt = false;
  if (!doPan && !doTilt) {
    doPan = true;
    panTarget = aimedPan(panHi);
  }

  uint32_t dur = scaleDur(durLo, durHi);
  uint32_t stagger = 0;
  if (doPan && doTilt && random(0, 100) < staggerPct) {
    stagger = 100 + static_cast<uint32_t>(random(0, 101));
  }
  if (doPan) startMove(pan, panTarget, dur, ease, 0, true, false);
  if (doTilt) startMove(tilt, tiltTarget, dur, ease, stagger, true, false);
  noteIdleMove(name, doPan, panTarget, doTilt, tiltTarget, dur, stagger);
  lastIdleWasHome = false;
  gesturesSinceHome++;
  nextIdleMs = 0;
}

static void serviceIdle(uint32_t now) {
  if (!axesSettled()) return;
  if (nextIdleMs == 0) {
    uint32_t span = idlePauseMax > idlePauseMin ? idlePauseMax - idlePauseMin : 0;
    uint32_t hold = idlePauseMin;
    if (span > 0) hold += static_cast<uint32_t>(random(0, static_cast<long>(span) + 1));
    if (random(0, 100) < idleLongPct) {
      hold += idlePauseMax / 2 + static_cast<uint32_t>(random(0, 2500));
    }
    hold += idleHoldBonus;
    idleHoldBonus = 0;
    nextIdleMs = now + hold;
    return;
  }
  if (static_cast<int32_t>(now - nextIdleMs) >= 0) planIdle(now);
}

static void unknown(const char* text) {
  Serial.print(F("unknown command \""));
  Serial.print(text);
  Serial.println(F("\" — type help"));
}

static void handleLine(char* text) {
  char raw[96];
  strncpy(raw, text, sizeof(raw) - 1);
  raw[sizeof(raw) - 1] = '\0';

  char* tok[8];
  int n = tokenize(text, tok, 8);
  if (n == 0) return;

  if (strcasecmp(tok[0], "help") == 0 && n == 1) {
    printHelp();
    return;
  }
  if (strcasecmp(tok[0], "status") == 0 && n == 1) {
    printStatus();
    return;
  }
  if (strcasecmp(tok[0], "ease") == 0 && n == 2) {
    Ease ease;
    if (!parseEase(tok[1], &ease)) {
      Serial.println(F("ease linear|cosine|smootherstep"));
      return;
    }
    defaultEase = ease;
    Serial.printf("default ease %s\n", easeName(ease));
    return;
  }
  if (strcasecmp(tok[0], "speed") == 0 && n == 2) {
    if (strcasecmp(tok[1], "quick") == 0) { defaultDurMs = 350; speedName = "quick"; }
    else if (strcasecmp(tok[1], "normal") == 0) { defaultDurMs = 800; speedName = "normal"; }
    else if (strcasecmp(tok[1], "slow") == 0) { defaultDurMs = 1600; speedName = "slow"; }
    else if (strcasecmp(tok[1], "subtle") == 0) { defaultDurMs = 2400; speedName = "subtle"; }
    else {
      Serial.println(F("speed quick|normal|slow|subtle"));
      return;
    }
    Serial.printf("default duration %lu ms (%s)\n", static_cast<unsigned long>(defaultDurMs),
                  speedName);
    return;
  }
  if (strcasecmp(tok[0], "ceiling") == 0 && n == 2) {
    if (strcasecmp(tok[1], "off") == 0) {
      ceilingDegPerSec = 0;
      Serial.println(F("ceiling off — duration is the move time"));
      return;
    }
    float cap = 0;
    if (!parseFloat(tok[1], &cap) || cap <= 0 || cap > 360) {
      Serial.println(F("ceiling <deg/s>|off"));
      return;
    }
    ceilingDegPerSec = cap;
    Serial.printf("ceiling %.0f deg/s — longer than the requested duration when needed\n", cap);
    return;
  }
  if (strcasecmp(tok[0], "axis") == 0 && n == 2) {
    Axis* axis = axisByName(tok[1]);
    if (axis == nullptr) {
      Serial.println(F("axis pan|tilt"));
      return;
    }
    selected = axis;
    Serial.printf("selected %s\n", axis->name);
    return;
  }

  const bool motion =
      strcasecmp(tok[0], "off") == 0 || strcasecmp(tok[0], "on") == 0 ||
      strcasecmp(tok[0], "stop") == 0 || strcasecmp(tok[0], "center") == 0 ||
      strcasecmp(tok[0], "c") == 0 || strcasecmp(tok[0], "us") == 0 ||
      strcasecmp(tok[0], "pan") == 0 || strcasecmp(tok[0], "tilt") == 0 ||
      strcasecmp(tok[0], "move") == 0 || strcasecmp(tok[0], "look") == 0 ||
      strcmp(tok[0], "+") == 0 || strcmp(tok[0], "-") == 0 || strcmp(tok[0], "++") == 0 ||
      strcmp(tok[0], "--") == 0;
  bool barePulse = false;
  if (n == 1) {
    char* end = nullptr;
    strtol(tok[0], &end, 10);
    barePulse = end != tok[0] && *end == '\0';
  }
  if (motion || barePulse) cancelAuto();

  if (strcasecmp(tok[0], "off") == 0 && n == 1) {
    pwmOff();
    return;
  }
  if (strcasecmp(tok[0], "on") == 0 && n == 1) {
    if (pwmEnable()) Serial.printf("pwm on, pan %d us, tilt %d us\n", pan.pulse, tilt.pulse);
    return;
  }
  if (strcasecmp(tok[0], "stop") == 0 && n == 1) {
    stopMotion();
    Serial.printf("stopped, pan %.1f deg, tilt %.1f deg\n", pan.angle, tilt.angle);
    return;
  }
  if (strcasecmp(tok[0], "demo") == 0 && n == 1) {
    if (!pwmOn && !pwmEnable()) return;
    beginDemo();
    return;
  }
  if (strcasecmp(tok[0], "idle") == 0) {
    if (n == 2 && strcasecmp(tok[1], "on") == 0) {
      if (!pwmOn && !pwmEnable()) return;
      beginIdle();
    } else if (n == 2 && strcasecmp(tok[1], "off") == 0) {
      mode = Mode::Manual;
      stopMotion();
      Serial.println(F("idle off"));
    } else if (n == 2 && (strcasecmp(tok[1], "calm") == 0 || strcasecmp(tok[1], "curious") == 0 ||
                          strcasecmp(tok[1], "lively") == 0)) {
      applyIdlePreset(tok[1]);
      if (mode == Mode::Idle) {
        Serial.printf("idle preset %s — scale %.2f, duration %.2f, hold %lu-%lu ms\n",
                      idlePresetName, idleAmp, idleDurScale,
                      static_cast<unsigned long>(idlePauseMin),
                      static_cast<unsigned long>(idlePauseMax));
      } else {
        if (!pwmOn && !pwmEnable()) return;
        beginIdle();
      }
    } else if (n == 3 && strcasecmp(tok[1], "scale") == 0) {
      float scale = 0;
      if (!parseFloat(tok[2], &scale) || scale < 0.3f || scale > 1.5f) {
        Serial.println(F("idle scale 0.3-1.5"));
        return;
      }
      idleAmp = scale;
      idlePresetName = "custom";
      Serial.printf("idle scale %.2f\n", idleAmp);
    } else if (n == 3 && strcasecmp(tok[1], "duration") == 0) {
      float scale = 0;
      if (!parseFloat(tok[2], &scale) || scale < 0.4f || scale > 2.0f) {
        Serial.println(F("idle duration 0.4-2.0"));
        return;
      }
      idleDurScale = scale;
      idlePresetName = "custom";
      Serial.printf("idle duration %.2f\n", idleDurScale);
    } else if (n == 4 && strcasecmp(tok[1], "pause") == 0) {
      long lo = 0;
      long hi = 0;
      if (!parseLong(tok[2], &lo) || !parseLong(tok[3], &hi) || lo < 200 || hi > 30000 ||
          lo > hi) {
        Serial.println(F("idle pause <minMs> <maxMs>, 200-30000, min <= max"));
        return;
      }
      idlePauseMin = static_cast<uint32_t>(lo);
      idlePauseMax = static_cast<uint32_t>(hi);
      idlePresetName = "custom";
      Serial.printf("idle hold %lu-%lu ms\n", static_cast<unsigned long>(idlePauseMin),
                    static_cast<unsigned long>(idlePauseMax));
    } else {
      Serial.println(F("idle on|off|calm|curious|lively"));
      Serial.println(F("idle scale <0.3-1.5> | duration <0.4-2> | pause <min> <max>"));
    }
    return;
  }
  if (strcasecmp(tok[0], "center") == 0 && n == 1) {
    startMove(pan, 0, defaultDurMs, defaultEase, 0, true, true);
    startMove(tilt, 0, defaultDurMs, defaultEase, 0, true, true);
    return;
  }
  if (strcasecmp(tok[0], "c") == 0 && n == 1) {
    // Fitted spline neutral: pan 1550 us, tilt 1500 us. Not an eased move.
    commandPulse(*selected, selected == &pan ? 1550 : 1500);
    return;
  }
  if ((strcmp(tok[0], "+") == 0 || strcmp(tok[0], "-") == 0 || strcmp(tok[0], "++") == 0 ||
       strcmp(tok[0], "--") == 0) &&
      n == 1) {
    int delta = 0;
    nudgeToken(tok[0], &delta);
    commandPulse(*selected, static_cast<long>(selected->pulse) + delta);
    return;
  }
  if (strcasecmp(tok[0], "us") == 0 && n == 3) {
    Axis* axis = axisByName(tok[1]);
    if (axis == nullptr) {
      Serial.println(F("us pan|tilt <us>|+| - |++|--"));
      return;
    }
    int delta = 0;
    if (nudgeToken(tok[2], &delta)) {
      commandPulse(*axis, static_cast<long>(axis->pulse) + delta);
      return;
    }
    long us = 0;
    if (!parseLong(tok[2], &us)) {
      Serial.println(F("us pan|tilt <us>|+| - |++|--"));
      return;
    }
    commandPulse(*axis, us);
    return;
  }
  if ((strcasecmp(tok[0], "pan") == 0 || strcasecmp(tok[0], "tilt") == 0) && n == 2) {
    Axis* axis = axisByName(tok[0]);
    float deg = 0;
    if (!parseFloat(tok[1], &deg)) {
      unknown(raw);
      return;
    }
    startMove(*axis, deg, defaultDurMs, defaultEase, 0, true, true);
    return;
  }
  if (strcasecmp(tok[0], "move") == 0 && (n == 5 || n == 7)) {
    Axis* axis = axisByName(tok[1]);
    float deg = 0;
    long ms = 0;
    Ease ease;
    long delayMs = 0;
    if (axis == nullptr || !parseFloat(tok[2], &deg) || !parseLong(tok[3], &ms) ||
        !parseEase(tok[4], &ease) || (n == 7 && (strcasecmp(tok[5], "after") != 0 ||
                                                 !parseLong(tok[6], &delayMs) || delayMs < 0))) {
      Serial.println(F("move pan|tilt <deg> <ms> <ease> [after <ms>]"));
      return;
    }
    startMove(*axis, deg, static_cast<uint32_t>(ms), ease, static_cast<uint32_t>(delayMs), true,
              true);
    return;
  }
  if (strcasecmp(tok[0], "look") == 0 && (n == 5 || n == 7)) {
    float panDeg = 0;
    float tiltDeg = 0;
    long ms = 0;
    Ease ease;
    long delayMs = 0;
    if (!parseFloat(tok[1], &panDeg) || !parseFloat(tok[2], &tiltDeg) || !parseLong(tok[3], &ms) ||
        !parseEase(tok[4], &ease) || (n == 7 && (strcasecmp(tok[5], "after") != 0 ||
                                                 !parseLong(tok[6], &delayMs) || delayMs < 0))) {
      Serial.println(F("look <panDeg> <tiltDeg> <ms> <ease> [after <ms>]"));
      return;
    }
    if (!angleAccepted(pan, panDeg) || !angleAccepted(tilt, tiltDeg)) return;
    startMove(pan, panDeg, static_cast<uint32_t>(ms), ease, 0, true, true);
    startMove(tilt, tiltDeg, static_cast<uint32_t>(ms), ease, static_cast<uint32_t>(delayMs), true,
              true);
    return;
  }

  long us = 0;
  if (n == 1 && parseLong(tok[0], &us)) {
    commandPulse(*selected, us);
    return;
  }
  unknown(raw);
}

void setup() {
  Serial.begin(115200);
  randomSeed(esp_random());
  // Two 50 Hz servos. GPIO25 and GPIO26 are both LEDC-capable on this ESP32.
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);

  // Fitted neutrals. Angle 0 through the firmware map is 1549 us on pan
  // because the trim is truncated to a whole microsecond; 1550 us is the horn.
  pan.pulse = 1550;
  tilt.pulse = 1500;
  pan.angle = angleForPulse(pan, pan.pulse);
  tilt.angle = angleForPulse(tilt, tilt.pulse);
  pwmOn = true;
  if (!pwmEnable()) pwmOn = false;

  lastTickMs = millis();
  Serial.println();
  printHelp();
  Serial.printf("holding neutral: pan %d us, tilt %d us, pwm %s\n", pan.pulse, tilt.pulse,
                pwmOn ? "on" : "off");
}

void loop() {
  static bool lastWasCr = false;
  while (Serial.available() > 0) {
    int rawCh = Serial.read();
    if (rawCh < 0) break;
    char c = static_cast<char>(rawCh);
    if (c == '\n' && lastWasCr) {
      lastWasCr = false;
      continue;
    }
    lastWasCr = (c == '\r');
    if (c == '\r' || c == '\n') {
      Serial.println();
      if (lineOverflow) Serial.println(F("line too long"));
      else {
        line[lineLen] = '\0';
        handleLine(line);
      }
      lineLen = 0;
      lineOverflow = false;
      continue;
    }
    if (c == '\b' || c == 0x7f) {
      if (lineLen > 0 && !lineOverflow) {
        --lineLen;
        Serial.print(F("\b \b"));
      }
      continue;
    }
    if (c < 0x20 || c >= 0x7f) continue;
    if (lineOverflow) continue;
    if (lineLen + 1 >= sizeof(line)) {
      lineOverflow = true;
      continue;
    }
    line[lineLen++] = c;
    Serial.print(c);
  }

  uint32_t now = millis();
  if (static_cast<uint32_t>(now - lastTickMs) >= kTickMs) {
    lastTickMs = now;
    updateAxis(pan, now);
    updateAxis(tilt, now);
    if (mode == Mode::Demo) advanceDemo(now);
    else if (mode == Mode::Idle) serviceIdle(now);
  }
}
