#include "idle_motion.h"

#include <strings.h>

// Curious is the laboratory default. Calm and Lively only change these
// four numbers; plan() below is the same choreography for all three.
// The laboratory's speed ceiling stayed off, so a planned duration is the
// move's duration.

static constexpr uint32_t kLeadInMs = 1200;
static constexpr uint32_t kCooldownMs = 5000;

static float rangePick(float lo, float hi) {
  if (hi < lo) {
    float swap = lo;
    lo = hi;
    hi = swap;
  }
  return lo + (hi - lo) * (static_cast<float>(random(0, 101)) / 100.0f);
}

void IdleMotion::begin(ServoNeck* pan, ServoNeck* tilt) {
  pan_ = pan;
  tilt_ = tilt;
}

void IdleMotion::suppress() {
  if (pan_ == nullptr || tilt_ == nullptr) return;
  bool active = pan_->trajectory() || tilt_->trajectory();
  pan_->cancelTrajectory();
  tilt_->cancelTrajectory();
  idleHoldBonus_ = 0;
  if (active) Serial.println(F("idle held"));
}

void IdleMotion::setEnabled(bool on) {
  if (enabled_ == on) return;
  enabled_ = on;
  if (!on) {
    suppress();
    return;
  }
  nextIdleMs_ = millis() + kLeadInMs;
  gesturesSinceHome_ = 0;
  lastIdleWasHome_ = false;
  idleHoldBonus_ = 0;
  lastPanSign_ = 0;
}

void IdleMotion::setGate(bool open) {
  if (gate_ == open) return;
  gate_ = open;
  if (!open) {
    suppress();
    return;
  }
  if (!enabled_) return;
  nextIdleMs_ = millis() + kLeadInMs;
  idleHoldBonus_ = 0;
}

void IdleMotion::setPreset(const char* name) {
  if (strcasecmp(name, "calm") == 0) {
    amp_ = 0.70f;
    durScale_ = 1.30f;
    pauseMin_ = 3500;
    pauseMax_ = 8000;
    longPct_ = 25;
    preset_ = "calm";
  } else if (strcasecmp(name, "lively") == 0) {
    amp_ = 1.10f;
    durScale_ = 0.80f;
    pauseMin_ = 1200;
    pauseMax_ = 3500;
    longPct_ = 8;
    preset_ = "lively";
  } else {
    // Preferred desk Curious: a little larger and quicker than the laboratory's
    // 1.0 / 1.0 / 2000–6000, still short of Lively.
    amp_ = 1.20f;
    durScale_ = 0.85f;
    pauseMin_ = 2500;
    pauseMax_ = 5500;
    longPct_ = 12;
    preset_ = "curious";
  }
}

void IdleMotion::setAmplitude(float scale) { amp_ = scale; }

void IdleMotion::setDurationScale(float scale) { durScale_ = scale; }

void IdleMotion::setPause(uint32_t minMs, uint32_t maxMs) {
  pauseMin_ = minMs;
  pauseMax_ = maxMs;
}

void IdleMotion::noteActivity(uint32_t nowMs) {
  if (!enabled_ || pan_ == nullptr || tilt_ == nullptr) return;
  // Only an idle moveTo() curve is cancelled. setTarget/setRaw already
  // cleared that flag on the commanded axis, so a tracking or look ease
  // keeps running.
  pan_->cancelTrajectory();
  tilt_->cancelTrajectory();
  idleHoldBonus_ = 0;
  nextIdleMs_ = nowMs + kCooldownMs;
}

void IdleMotion::printStatus(bool connected, bool asleep, bool speaking) const {
  Serial.printf("idle      %s\n", enabled_ ? "on" : "off");
  Serial.printf("preset    %s  scale %.2f  duration %.2f  hold %lu-%lu ms  long %d%%\n",
                preset_, amp_, durScale_, static_cast<unsigned long>(pauseMin_),
                static_cast<unsigned long>(pauseMax_), longPct_);
  const char* block = nullptr;
  if (!enabled_) block = "idle off";
  else if (!connected) block = "brain disconnected";
  else if (asleep) block = "asleep";
  if (block != nullptr) {
    Serial.printf("gate      blocked: %s\n", block);
  } else if (pan_ != nullptr && (pan_->trajectory() || tilt_->trajectory())) {
    Serial.println(F("gate      open, gesture in progress"));
  } else if (speaking) {
    Serial.println(F("gate      open, paused while speaking"));
  } else {
    uint32_t now = millis();
    if (nextIdleMs_ != 0 && static_cast<int32_t>(nextIdleMs_ - now) > 0) {
      Serial.printf("gate      open, next gesture in %lu ms\n",
                    static_cast<unsigned long>(nextIdleMs_ - now));
    } else {
      Serial.println(F("gate      open"));
    }
  }
  Serial.println(F("tracking  leave face tracking off; a still face sends no pan/tilt"));
}

float IdleMotion::clampAxis(const ServoNeck& axis, float deg) const {
  if (deg < axis.minDeg()) return axis.minDeg();
  if (deg > axis.maxDeg()) return axis.maxDeg();
  return deg;
}

uint32_t IdleMotion::scaleDur(uint32_t lo, uint32_t hi) const {
  uint32_t base = lo + static_cast<uint32_t>(random(0, static_cast<long>(hi - lo) + 1));
  float scaled = static_cast<float>(base) * durScale_;
  if (scaled < 250.0f) scaled = 250.0f;
  if (scaled > 8000.0f) scaled = 8000.0f;
  return static_cast<uint32_t>(scaled + 0.5f);
}

float IdleMotion::aimedPan(float mag) {
  float signedMag = fabsf(mag) * amp_;
  int dir = lastPanSign_ > 0 ? -1 : (lastPanSign_ < 0 ? 1 : (pan_->current() >= 0.0f ? -1 : 1));
  lastPanSign_ = dir;
  return clampAxis(*pan_, dir * signedMag);
}

void IdleMotion::noteMove(const char* name, bool doPan, float panTarget, bool doTilt,
                          float tiltTarget, uint32_t dur, uint32_t stagger) const {
  Serial.print(F("[idle] "));
  Serial.print(name);
  Serial.print(':');
  if (doPan) Serial.printf(" pan %+.0f deg", panTarget);
  if (doTilt) Serial.printf(" tilt %+.0f deg", tiltTarget);
  Serial.printf(", %lu ms", static_cast<unsigned long>(dur));
  if (stagger > 0) Serial.printf(", tilt +%lu ms", static_cast<unsigned long>(stagger));
  Serial.println();
}

void IdleMotion::armHold(uint32_t nowMs) {
  uint32_t span = pauseMax_ > pauseMin_ ? pauseMax_ - pauseMin_ : 0;
  uint32_t hold = pauseMin_;
  if (span > 0) hold += static_cast<uint32_t>(random(0, static_cast<long>(span) + 1));
  if (random(0, 100) < longPct_) {
    hold += pauseMax_ / 2 + static_cast<uint32_t>(random(0, 2500));
  }
  hold += idleHoldBonus_;
  idleHoldBonus_ = 0;
  nextIdleMs_ = nowMs + hold;
}

void IdleMotion::plan(uint32_t nowMs) {
  bool goHome = !lastIdleWasHome_ && gesturesSinceHome_ >= 2 && random(0, 100) < 18;
  if (goHome && fabsf(pan_->current()) < 3.0f && fabsf(tilt_->current()) < 3.0f) goHome = false;
  if (goHome) {
    bool doPan = fabsf(pan_->current()) >= 3.0f;
    bool doTilt = fabsf(tilt_->current()) >= 3.0f;
    uint32_t dur = scaleDur(800, 1100);
    if (doPan) pan_->moveTo(0, dur, ServoNeck::Ease::Cosine, 0, nowMs);
    if (doTilt) tilt_->moveTo(0, dur, ServoNeck::Ease::Cosine, 0, nowMs);
    noteMove("return", doPan, 0, doTilt, 0, dur, 0);
    lastIdleWasHome_ = true;
    gesturesSinceHome_ = 0;
    lastPanSign_ = 0;
    nextIdleMs_ = 0;
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
  ServoNeck::Ease ease = ServoNeck::Ease::Cosine;
  int staggerPct = 40;
  if (gesture == 1) {
    name = "thoughtful look";
    panLo = 10.0f;
    panHi = 25.0f;
    tiltLo = -25.0f;
    tiltHi = -15.0f;
    durLo = 1000;
    durHi = 1500;
    ease = ServoNeck::Ease::Smooth;
    staggerPct = 20;
    idleHoldBonus_ = 1500 + static_cast<uint32_t>(random(0, 1500));
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
    ease = ServoNeck::Ease::Smooth;
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
    if (fabsf(panTarget - pan_->current()) < 6.0f) {
      float pushed = fabsf(panHi) * amp_;
      if (pushed < 12.0f) pushed = 12.0f;
      panTarget = clampAxis(*pan_, static_cast<float>(lastPanSign_) * pushed);
    }
  }
  if (doTilt) tiltTarget = clampAxis(*tilt_, rangePick(tiltLo, tiltHi) * amp_);
  if (doPan && fabsf(panTarget - pan_->current()) < 3.0f) doPan = false;
  if (doTilt && fabsf(tiltTarget - tilt_->current()) < 3.0f) doTilt = false;
  if (!doPan && !doTilt) {
    doPan = true;
    panTarget = aimedPan(panHi);
  }

  uint32_t dur = scaleDur(durLo, durHi);
  uint32_t stagger = 0;
  if (doPan && doTilt && random(0, 100) < staggerPct) {
    stagger = 100 + static_cast<uint32_t>(random(0, 101));
  }
  if (doPan) pan_->moveTo(panTarget, dur, ease, 0, nowMs);
  if (doTilt) tilt_->moveTo(tiltTarget, dur, ease, stagger, nowMs);
  noteMove(name, doPan, panTarget, doTilt, tiltTarget, dur, stagger);
  lastIdleWasHome_ = false;
  gesturesSinceHome_++;
  nextIdleMs_ = 0;
}

void IdleMotion::service(uint32_t nowMs, bool speaking) {
  if (!enabled_ || !gate_ || pan_ == nullptr || tilt_ == nullptr) return;
  // Powered through the hold, so the 1.5 s relax cannot drop a pose.
  pan_->hold();
  tilt_->hold();
  if (pan_->moving() || tilt_->moving()) return;
  if (nextIdleMs_ == 0) {
    armHold(nowMs);
    return;
  }
  // A gesture already underway finished above. Don't start another yet.
  if (speaking) return;
  if (static_cast<int32_t>(nowMs - nextIdleMs_) >= 0) plan(nowMs);
}
