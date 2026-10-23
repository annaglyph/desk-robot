#include "servo_neck.h"

#include "config.h"

// attach() always stores a 10-bit tick count. The library applies a
// different width only when setTimerWidth() runs afterwards, which is what
// the calibrator does. That call restarts the shared timer and clears both
// outputs, so every attached axis has to be written again.
static constexpr int kTimerBits = 16;

// Manual moves close 15% of the remaining error per step at 33 ms. Scale that
// fraction with the servo interval so the same move takes the same time:
// 1 - 0.85^(interval/33). The 0.15° anti-stall step scales by the same ratio.
// At 20 ms those are 0.0938 and 0.0909°. The deg/s caps already use dt.
static const float kManualFraction =
    1.0f - powf(0.85f, static_cast<float>(SERVO_INTERVAL_MS) / 33.0f);
static const float kMinStepDeg = 0.15f * (static_cast<float>(SERVO_INTERVAL_MS) / 33.0f);
static ServoNeck* necks_[2];
static int neckCount_ = 0;

static float easeValue(ServoNeck::Ease ease, float t) {
  if (t <= 0.0f) return 0.0f;
  if (t >= 1.0f) return 1.0f;
  if (ease == ServoNeck::Ease::Cosine) return (1.0f - cosf(PI * t)) * 0.5f;
  // Smootherstep: 6t^5 - 15t^4 + 10t^3. Same curve as the motion laboratory.
  float t2 = t * t;
  float t3 = t2 * t;
  return t3 * (t * (t * 6.0f - 15.0f) + 10.0f);
}

void ServoNeck::rewriteAttached() {
  for (int i = 0; i < neckCount_; ++i) {
    if (necks_[i]->attached_) necks_[i]->writeAngle(necks_[i]->currentDeg_);
  }
}

// Printed from the first pan/tilt/raw command, once USB serial is being read.
void ServoNeck::reportTimerWidthOnce() {
  static bool reported = false;
  if (reported) return;
  reported = true;
  Serial.print(F("servo: timer width"));
  for (int i = 0; i < neckCount_; ++i) {
    Serial.printf(" pin %u=%d", necks_[i]->pin_, necks_[i]->servo_.readTimerWidth());
  }
  Serial.println();
}

void ServoNeck::begin(uint8_t pin, float minDeg, float maxDeg,
                      float maxSpeedDegPerSec, uint32_t relaxAfterMs,
                      float trimDeg, bool invert) {
  pin_ = pin;
  minDeg_ = minDeg;
  maxDeg_ = maxDeg;
  maxSpeed_ = maxSpeedDegPerSec;
  relaxAfterMs_ = relaxAfterMs;
  trimDeg_ = trimDeg;
  invert_ = invert;
  if (neckCount_ < 2) necks_[neckCount_++] = this;
  currentDeg_ = 0;
  targetDeg_ = 0;
  lastUpdateMs_ = millis();
  attachIfNeeded();
  writeAngle(0);
}

void ServoNeck::attachIfNeeded() {
  if (attached_) return;
  servo_.setPeriodHertz(50);
  // 500-2400us covers the full travel of SG90/MG90S-class servos.
  servo_.attach(pin_, 500, 2400);
  servo_.setTimerWidth(kTimerBits);
  attached_ = true;
  rewriteAttached();
}

void ServoNeck::writeAngle(float deg) {
  // Head degrees (-90..90) → servo microseconds, 0 head-deg = servo center.
  float servoDeg = (invert_ ? -deg : deg) + trimDeg_;
  float t = (servoDeg + 90.0f) / 180.0f;
  int us = 500 + static_cast<int>(t * 1900.0f);
  servo_.writeMicroseconds(constrain(us, 500, 2400));
}

void ServoNeck::clearTrajectory() {
  timed_ = false;
  pending_ = false;
}

void ServoNeck::setRaw(float deg) {
  clearTrajectory();
  deg = constrain(deg, -60.0f, 60.0f);
  attachIfNeeded();
  currentDeg_ = targetDeg_ = deg;
  settledSinceMs_ = 0;
  writeAngle(deg);
  reportTimerWidthOnce();
}

void ServoNeck::hold() {
  bool wasAttached = attached_;
  attachIfNeeded();
  if (!wasAttached) writeAngle(currentDeg_);  // put it back where it belongs
  settledSinceMs_ = 0;                        // and don't relax while held
}

void ServoNeck::setTarget(float deg) {
  // An explicit target replaces a timed curve. The eased path below then
  // runs from the angle already reached, so the head does not jump.
  clearTrajectory();
  targetDeg_ = constrain(deg, minDeg_, maxDeg_);
  settledSinceMs_ = 0;
  attachIfNeeded();
  reportTimerWidthOnce();
}

void ServoNeck::moveTo(float deg, uint32_t durationMs, Ease ease, uint32_t delayMs,
                       uint32_t nowMs) {
  deg = constrain(deg, minDeg_, maxDeg_);
  if (durationMs < 1) durationMs = 1;
  if (durationMs > 30000) durationMs = 30000;
  timedTo_ = deg;
  timedEase_ = ease;
  timedDurMs_ = durationMs;
  settledSinceMs_ = 0;
  attachIfNeeded();
  if (delayMs > 0) {
    timed_ = false;
    pending_ = true;
    pendingAt_ = nowMs + delayMs;
    return;
  }
  pending_ = false;
  timed_ = true;
  timedFrom_ = currentDeg_;
  targetDeg_ = deg;
  timedStartMs_ = nowMs;
}

void ServoNeck::cancelTrajectory() {
  if (!timed_ && !pending_) return;
  clearTrajectory();
  targetDeg_ = currentDeg_;
  settledSinceMs_ = 0;
}

void ServoNeck::update(uint32_t nowMs) {
  float dt = (nowMs - lastUpdateMs_) / 1000.0f;
  lastUpdateMs_ = nowMs;
  if (dt <= 0 || dt > 0.5f) dt = SERVO_INTERVAL_MS / 1000.0f;

  if (pending_ && static_cast<int32_t>(nowMs - pendingAt_) >= 0) {
    pending_ = false;
    timed_ = true;
    timedFrom_ = currentDeg_;
    targetDeg_ = timedTo_;
    timedStartMs_ = nowMs;
  }

  if (timed_) {
    uint32_t elapsed = nowMs - timedStartMs_;
    float t = timedDurMs_ == 0
                  ? 1.0f
                  : static_cast<float>(elapsed) / static_cast<float>(timedDurMs_);
    attachIfNeeded();
    if (t >= 1.0f) {
      currentDeg_ = timedTo_;
      targetDeg_ = timedTo_;
      timed_ = false;
      writeAngle(currentDeg_);
      if (settledSinceMs_ == 0) settledSinceMs_ = nowMs;
    } else {
      currentDeg_ = timedFrom_ + (timedTo_ - timedFrom_) * easeValue(timedEase_, t);
      writeAngle(currentDeg_);
      settledSinceMs_ = 0;
    }
    return;
  }
  if (pending_) return;

  float diff = targetDeg_ - currentDeg_;
  if (fabsf(diff) > 0.25f) {
    // Ease-out toward the target, capped at maxSpeed_.
    float step = diff * kManualFraction;
    float maxStep = maxSpeed_ * dt;
    step = constrain(step, -maxStep, maxStep);
    // Always make progress so the tail of the ease doesn't stall.
    if (fabsf(step) < kMinStepDeg) step = (diff > 0 ? kMinStepDeg : -kMinStepDeg);
    currentDeg_ += step;
    attachIfNeeded();
    writeAngle(currentDeg_);
    settledSinceMs_ = 0;
  } else if (attached_) {
    if (settledSinceMs_ == 0) {
      settledSinceMs_ = nowMs;
    } else if (nowMs - settledSinceMs_ >= relaxAfterMs_) {
      // At rest long enough: stop driving the servo so it doesn't buzz.
      servo_.detach();
      attached_ = false;
    }
  }
}
