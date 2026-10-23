#pragma once

#include <Arduino.h>
#include <ESP32Servo.h>

// One head axis (pan or tilt): smooth easing, speed limiting, auto-relax.
//
// Angles are in "head degrees": 0 = centered. For pan, negative = robot's
// left; for tilt, negative = down. The class maps that onto the servo's
// 0-180 range, with an optional trim offset and direction flip.

class ServoNeck {
 public:
  void begin(uint8_t pin, float minDeg, float maxDeg, float maxSpeedDegPerSec,
             uint32_t relaxAfterMs, float trimDeg = 0.0f,
             float glanceRangeDeg = 25.0f, bool invert = false);

  void setTarget(float deg);
  // Calibration only: drive straight to a head angle, ignoring the limits.
  // Use it to find which way the servo turns before trusting the limits.
  void setRaw(float deg);
  // Keep this axis powered at its current angle. An unpowered servo sags
  // under the head's weight when the other axis swings; call this while the
  // other axis is moving.
  void hold();
  bool moving() const { return fabsf(targetDeg_ - currentDeg_) > 0.25f; }
  float current() const { return currentDeg_; }
  float target() const { return targetDeg_; }

  // When idle glances are on, the head occasionally turns a little on its
  // own — paired with the face saccades it makes the robot feel alive.
  void setIdleGlances(bool on) { idleGlances_ = on; }

  // Call every frame.
  void update(uint32_t nowMs);

 private:
  void writeAngle(float deg);
  void attachIfNeeded();
  void rewriteAttached();
  void reportTimerWidthOnce();

  Servo servo_;
  uint8_t pin_ = 0;
  float minDeg_ = -60, maxDeg_ = 60;
  float maxSpeed_ = 180; // deg/sec
  uint32_t relaxAfterMs_ = 1500;
  float trimDeg_ = 0;
  float glanceRange_ = 25;
  bool invert_ = false;

  float currentDeg_ = 0;
  float targetDeg_ = 0;
  bool attached_ = false;
  uint32_t settledSinceMs_ = 0;
  uint32_t lastUpdateMs_ = 0;
  uint32_t nextGlanceMs_ = 0;
  bool idleGlances_ = true;
};
