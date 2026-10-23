#pragma once

#include <Arduino.h>

#include "servo_neck.h"

// Idle choreography ported from the motion laboratory
// (tools/servo-calibrator/src/main.cpp). This asks ServoNeck for timed
// moves. It does not write pulses itself.
//
// The on/off switch starts off. Motion also waits until the brain is
// connected and Rocky is awake. Sleep, glance off, idle off, and a brain
// disconnect stop a gesture where it is.
//
// The brain owns the switch. It turns idle off for tracking, a still face
// included, and reads the held pose back from the idle_ack to `glance off`.
// Switched on from the serial console, nothing holds idle back for a face.

class IdleMotion {
 public:
  void begin(ServoNeck* pan, ServoNeck* tilt);

  void setEnabled(bool on);
  bool enabled() const { return enabled_; }
  void setPreset(const char* name);
  // Brain connected and awake. Closing the gate stops a gesture immediately.
  void setGate(bool open);

  // A pan, tilt, center, or raw command. The caller has already replaced
  // that axis. This drops the other axis's idle curve and waits 5 s.
  void noteActivity(uint32_t nowMs);

  // Call once per servo tick, after ServoNeck::update.
  void service(uint32_t nowMs, bool speaking);
  void printStatus(bool connected, bool asleep, bool speaking) const;

  const char* preset() const { return preset_; }
  float amplitude() const { return amp_; }
  float durationScale() const { return durScale_; }
  uint32_t pauseMin() const { return pauseMin_; }
  uint32_t pauseMax() const { return pauseMax_; }

 private:
  float clampAxis(const ServoNeck& axis, float deg) const;
  uint32_t scaleDur(uint32_t lo, uint32_t hi) const;
  float aimedPan(float mag);
  void noteMove(const char* name, bool doPan, float panTarget, bool doTilt,
                float tiltTarget, uint32_t dur, uint32_t stagger) const;
  void armHold(uint32_t nowMs);
  void plan(uint32_t nowMs);
  void suppress();

  ServoNeck* pan_ = nullptr;
  ServoNeck* tilt_ = nullptr;

  bool enabled_ = false;
  bool gate_ = false;
  const char* preset_ = "curious";
  float amp_ = 1.0f;
  float durScale_ = 1.0f;
  uint32_t pauseMin_ = 2000;
  uint32_t pauseMax_ = 6000;
  int longPct_ = 12;

  int lastPanSign_ = 0;
  uint32_t nextIdleMs_ = 0;
  int gesturesSinceHome_ = 0;
  bool lastIdleWasHome_ = false;
  uint32_t idleHoldBonus_ = 0;
};
