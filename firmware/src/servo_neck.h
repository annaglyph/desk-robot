#pragma once

#include <Arduino.h>
#include <ESP32Servo.h>

// One head axis (pan or tilt): smooth easing, speed limiting, auto-relax.
//
// Angles are in "head degrees": 0 = centered. For pan, negative = robot's
// left; for tilt, negative = down. The class maps that onto the servo's
// 0-180 range, with an optional trim offset and direction flip.
//
// setTarget eases a fraction of the remaining error each update. moveTo plays
// a timed curve from the current angle. An explicit setTarget or setRaw, or
// cancelTrajectory, drops that curve on the next update.

class ServoNeck {
 public:
  enum class Ease : uint8_t { Cosine, Smooth };

  void begin(uint8_t pin, float minDeg, float maxDeg, float maxSpeedDegPerSec,
             uint32_t relaxAfterMs, float trimDeg = 0.0f, bool invert = false);

  void setTarget(float deg);
  // Calibration only: drive straight to a head angle, ignoring the limits.
  // Use it to find which way the servo turns before trusting the limits.
  void setRaw(float deg);
  // Keep this axis powered at its current angle. An unpowered servo sags
  // under the head's weight when the other axis swings; call this while the
  // other axis is moving.
  void hold();
  // Timed move from the live angle. delayMs waits before starting, so tilt
  // can follow pan. Duration is the whole move, not a speed.
  void moveTo(float deg, uint32_t durationMs, Ease ease, uint32_t delayMs,
              uint32_t nowMs);
  // Drop a timed move and keep the angle already reached.
  void cancelTrajectory();

  bool moving() const {
    return timed_ || pending_ || fabsf(targetDeg_ - currentDeg_) > 0.25f;
  }
  bool trajectory() const { return timed_ || pending_; }
  float current() const { return currentDeg_; }
  float target() const { return targetDeg_; }
  float minDeg() const { return minDeg_; }
  float maxDeg() const { return maxDeg_; }

  // Call every frame.
  void update(uint32_t nowMs);

 private:
  void writeAngle(float deg);
  void attachIfNeeded();
  void rewriteAttached();
  void reportTimerWidthOnce();
  void clearTrajectory();

  Servo servo_;
  uint8_t pin_ = 0;
  float minDeg_ = -60, maxDeg_ = 60;
  float maxSpeed_ = 180; // deg/sec
  uint32_t relaxAfterMs_ = 1500;
  float trimDeg_ = 0;
  bool invert_ = false;

  float currentDeg_ = 0;
  float targetDeg_ = 0;
  bool attached_ = false;
  uint32_t settledSinceMs_ = 0;
  uint32_t lastUpdateMs_ = 0;

  bool timed_ = false;
  bool pending_ = false;
  uint32_t timedStartMs_ = 0;
  uint32_t timedDurMs_ = 0;
  uint32_t pendingAt_ = 0;
  float timedFrom_ = 0;
  float timedTo_ = 0;
  Ease timedEase_ = Ease::Cosine;
};
