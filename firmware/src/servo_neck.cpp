#include "servo_neck.h"

// attach() always stores a 10-bit tick count. The library applies a
// different width only when setTimerWidth() runs afterwards, which is what
// the calibrator does. That call restarts the shared timer and clears both
// outputs, so every attached axis has to be written again.
static constexpr int kTimerBits = 16;
static ServoNeck* necks_[2];
static int neckCount_ = 0;

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
                      float trimDeg, float glanceRangeDeg, bool invert) {
  pin_ = pin;
  minDeg_ = minDeg;
  maxDeg_ = maxDeg;
  maxSpeed_ = maxSpeedDegPerSec;
  relaxAfterMs_ = relaxAfterMs;
  trimDeg_ = trimDeg;
  glanceRange_ = glanceRangeDeg;
  invert_ = invert;
  if (neckCount_ < 2) necks_[neckCount_++] = this;
  currentDeg_ = 0;
  targetDeg_ = 0;
  lastUpdateMs_ = millis();
  nextGlanceMs_ = millis() + 5000;
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

void ServoNeck::setRaw(float deg) {
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
  targetDeg_ = constrain(deg, minDeg_, maxDeg_);
  settledSinceMs_ = 0;
  attachIfNeeded();
  reportTimerWidthOnce();
}

void ServoNeck::update(uint32_t nowMs) {
  float dt = (nowMs - lastUpdateMs_) / 1000.0f;
  lastUpdateMs_ = nowMs;
  if (dt <= 0 || dt > 0.5f) dt = 0.033f;

  if (idleGlances_ && nowMs >= nextGlanceMs_) {
    // Small wander, biased back toward center.
    int range = static_cast<int>(glanceRange_);
    float glance = static_cast<float>(random(-range, range + 1));
    if (random(100) < 50) glance = 0;
    setTarget(glance);
    nextGlanceMs_ = nowMs + random(6000, 16000);
  }

  float diff = targetDeg_ - currentDeg_;
  if (fabsf(diff) > 0.25f) {
    // Ease-out toward the target, capped at maxSpeed_.
    float step = diff * 0.15f;
    float maxStep = maxSpeed_ * dt;
    step = constrain(step, -maxStep, maxStep);
    // Always make progress so the tail of the ease doesn't stall.
    if (fabsf(step) < 0.15f) step = (diff > 0 ? 0.15f : -0.15f);
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
