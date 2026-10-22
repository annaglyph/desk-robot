#include "face.h"

namespace {

constexpr int SCREEN_W = 128;
constexpr int SCREEN_H = 64;
constexpr int EYE_GAP = 16; // space between the two eyes

const char* kEmotionNames[] = {
    "neutral", "happy", "sad", "angry", "surprised", "sleepy", "thinking",
};

float approach(float cur, float target, float factor) {
  return cur + (target - cur) * factor;
}

} // namespace

const char* emotionName(Emotion e) {
  return kEmotionNames[static_cast<uint8_t>(e)];
}

bool emotionFromName(const char* name, Emotion& out) {
  for (uint8_t i = 0; i < static_cast<uint8_t>(Emotion::COUNT); i++) {
    if (strcasecmp(name, kEmotionNames[i]) == 0) {
      out = static_cast<Emotion>(i);
      return true;
    }
  }
  return false;
}

Face::Params Face::paramsFor(Emotion e) {
  //                     eyeW  eyeH  radius browSlant lowerLid upperLid
  switch (e) {
    case Emotion::Happy:     return {36, 36, 12,  0, 16,  0};
    case Emotion::Sad:       return {32, 26, 10, -9,  0,  4};
    case Emotion::Angry:     return {34, 24,  8, 10,  0,  0};
    case Emotion::Surprised: return {38, 44, 19,  0,  0,  0};
    case Emotion::Sleepy:    return {34, 26, 10,  0,  0, 14};
    case Emotion::Thinking:  return {30, 30, 11,  0,  0,  5};
    case Emotion::Neutral:
    default:                 return {34, 34, 12,  0,  0,  0};
  }
}

void Face::begin() {
  u8g2_.begin();
  u8g2_.setBusClock(400000);
  // Wake-up: start with eyes shut, then open.
  blinkAmount_ = 1.0f;
  blinkClosing_ = false;
  nextBlinkMs_ = millis() + 3000;
  nextSaccadeMs_ = millis() + 1500;
  nextSquintMs_ = millis() + 9000;
}

void Face::setEmotion(Emotion e) {
  if (e != emotion_) {
    pop_ = 1.0f;  // a little overshoot sells the change
    emotionSinceMs_ = millis();
    tearY_ = -1.0f;
    nextTearMs_ = millis() + 1500;
    thinkDots_ = 0;
  }
  emotion_ = e;
  target_ = paramsFor(e);
  // Thinking looks up and to the side; other emotions release the gaze.
  if (e == Emotion::Thinking) {
    gazeTargetX_ = 7;
    gazeTargetY_ = -5;
  } else {
    gazeTargetX_ = 0;
    gazeTargetY_ = 0;
  }
}

void Face::blink() { blinkClosing_ = true; }

void Face::setAsleep(bool on) {
  if (on == asleep_) return;
  asleep_ = on;
  if (on) {
    for (Zed& z : zeds_) z.alive = false;
    nextZedMs_ = millis() + 800;
    nextTwitchMs_ = millis() + random(5000, 9000);
    gazeTargetX_ = gazeTargetY_ = 0;
  } else {
    pop_ = 1.0f;  // eyes spring open
    blinkAmount_ = 0.0f;
  }
}

void Face::setTalking(bool on, float level) {
  talking_ = on;
  mouthLevel_ = constrain(level, 0.0f, 1.0f);
}

void Face::stepAnimation(uint32_t nowMs) {
  frame_++;

  // Morph eye shape toward the current emotion preset (or shut, if asleep).
  Params goal = asleep_ ? Params{30, 3, 1, 0, 0, 0} : target_;
  float f = asleep_ ? 0.12f : 0.25f;  // eyes close slowly, change fast
  cur_.eyeW = approach(cur_.eyeW, goal.eyeW, f);
  cur_.eyeH = approach(cur_.eyeH, goal.eyeH, f);
  cur_.radius = approach(cur_.radius, goal.radius, f);
  cur_.browSlant = approach(cur_.browSlant, goal.browSlant, f);
  cur_.lowerLid = approach(cur_.lowerLid, goal.lowerLid, f);
  cur_.upperLid = approach(cur_.upperLid, goal.upperLid, f);

  pop_ *= 0.82f;
  squint_ *= 0.85f;

  if (asleep_) {
    // Slow breathing; the odd dream twitch; Z's drifting up.
    breath_ += 0.035f;
    blinkAmount_ = 0.0f;
    blinkClosing_ = false;
    twitch_ *= 0.8f;
    if (nowMs >= nextTwitchMs_) {
      twitch_ = 1.0f;
      nextTwitchMs_ = nowMs + random(6000, 12000);
    }
    if (nowMs >= nextZedMs_) {
      for (Zed& z : zeds_) {
        if (!z.alive) {
          z.alive = true;
          z.age = 0;
          z.x = SCREEN_W / 2 + EYE_GAP / 2 + cur_.eyeW + 2;
          z.y = SCREEN_H / 2 - 6;
          break;
        }
      }
      nextZedMs_ = nowMs + random(1100, 1700);
    }
    for (Zed& z : zeds_) {
      if (!z.alive) continue;
      z.age += 0.012f;
      z.y -= 0.32f;
      z.x += 0.22f;
      if (z.age >= 1.0f || z.y < -12) z.alive = false;
    }
    gazeX_ = approach(gazeX_, 0, 0.2f);
    gazeY_ = approach(gazeY_, 0, 0.2f);
    mouth_ = approach(mouth_, 0, 0.3f);
    return;
  }

  // Blink: snap shut fast, reopen a little slower — reads as natural.
  if (blinkClosing_) {
    blinkAmount_ += 0.45f;
    if (blinkAmount_ >= 1.0f) {
      blinkAmount_ = 1.0f;
      blinkClosing_ = false;
    }
  } else if (blinkAmount_ > 0.0f) {
    blinkAmount_ = max(0.0f, blinkAmount_ - 0.28f);
  }

  if (idle_) {
    if (nowMs >= nextBlinkMs_) {
      blinkClosing_ = true;
      nextBlinkMs_ = nowMs + random(2200, 6000);
      // Occasional double blink.
      if (random(100) < 20) nextBlinkMs_ = nowMs + 400;
    }
    if (nowMs >= nextSaccadeMs_ && emotion_ != Emotion::Thinking) {
      gazeTargetX_ = static_cast<float>(random(-8, 9));
      gazeTargetY_ = static_cast<float>(random(-4, 5));
      // Mostly return to center so the robot doesn't look shifty.
      if (random(100) < 40) gazeTargetX_ = gazeTargetY_ = 0;
      nextSaccadeMs_ = nowMs + random(1200, 4000);
    }
    if (nowMs >= nextSquintMs_ && emotion_ == Emotion::Neutral) {
      squint_ = 1.0f;
      nextSquintMs_ = nowMs + random(8000, 16000);
    }
  }

  // Per-emotion flourishes.
  if (emotion_ == Emotion::Sad) {
    if (tearY_ < 0 && nowMs >= nextTearMs_) tearY_ = 0;
    if (tearY_ >= 0) {
      tearY_ += 0.7f;
      if (tearY_ > SCREEN_H) {
        tearY_ = -1;
        nextTearMs_ = nowMs + random(2500, 5000);
      }
    }
  }
  if (emotion_ == Emotion::Thinking && nowMs >= nextDotMs_) {
    thinkDots_ = (thinkDots_ + 1) % 4;
    nextDotMs_ = nowMs + 420;
  }
  jitterX_ = (emotion_ == Emotion::Angry && (frame_ % 3 == 0)) ? random(-1, 2) : 0;

  // Mouth follows the speaker's loudness while talking, closes otherwise.
  float mouthGoal = talking_ ? constrain(0.15f + mouthLevel_ * 4.0f, 0.15f, 1.0f) : 0.0f;
  mouth_ = approach(mouth_, mouthGoal, talking_ ? 0.5f : 0.3f);

  gazeX_ = approach(gazeX_, gazeTargetX_, 0.35f);
  gazeY_ = approach(gazeY_, gazeTargetY_, 0.35f);
}

void Face::drawEye(int cx, int cy, bool isLeft, float hScale) {
  // Eye height collapses as the blink progresses.
  float h = cur_.eyeH * hScale * (1.0f - blinkAmount_);
  if (h < 2) h = 2;
  float w = cur_.eyeW;

  int x = cx - static_cast<int>(w / 2);
  int y = cy - static_cast<int>(h / 2);
  int r = min(static_cast<int>(cur_.radius), static_cast<int>(min(w, h) / 2 - 1));
  if (r < 0) r = 0;

  u8g2_.setDrawColor(1);
  u8g2_.drawRBox(x, y, static_cast<int>(w), static_cast<int>(h), r);

  // Overlays are drawn in black to carve the eye shape.
  u8g2_.setDrawColor(0);

  // Brow: a triangle clipped off the top edge. Slant > 0 cuts the inner
  // corner (angry); slant < 0 cuts the outer corner (sad).
  float slant = cur_.browSlant;
  if (fabsf(slant) > 0.5f) {
    int depth = static_cast<int>(fabsf(slant));
    bool cutInner = (slant > 0);
    // "Inner" is the right edge of the left eye, left edge of the right eye.
    bool cutRightCorner = isLeft ? cutInner : !cutInner;
    int x0 = x - 1, x1 = x + static_cast<int>(w) + 1;
    if (cutRightCorner) {
      u8g2_.drawTriangle(x0, y - 1, x1, y - 1, x1, y + depth);
    } else {
      u8g2_.drawTriangle(x0, y - 1, x1, y - 1, x0, y + depth);
    }
  }

  // Lower lid: a disc pushed up from below turns the eye into a happy crescent.
  if (cur_.lowerLid > 0.5f) {
    int lidR = static_cast<int>(w);
    int lidY = y + static_cast<int>(h) + lidR - static_cast<int>(cur_.lowerLid);
    u8g2_.drawDisc(cx, lidY, lidR);
  }

  // Upper lid: a flat droop from above for sleepy/thinking.
  if (cur_.upperLid > 0.5f) {
    u8g2_.drawBox(x - 1, y - 1, static_cast<int>(w) + 2,
                  static_cast<int>(cur_.upperLid) + 1);
  }

  // Glint: a small dark square high in the eye, offset by the gaze so it
  // reads as a reflection. Skipped when the eye is nearly shut.
  if (h > 12 && !asleep_) {
    int gx = x + static_cast<int>(w * 0.22f) + static_cast<int>(gazeX_ * 0.3f);
    int gy = y + static_cast<int>(h * 0.18f) + static_cast<int>(cur_.upperLid);
    int gs = (w > 34) ? 4 : 3;
    u8g2_.drawBox(gx, gy, gs, gs);
  }

  u8g2_.setDrawColor(1);
}

void Face::drawMouth(int cx, int cy) {
  if (mouth_ < 0.05f) return;
  int mh = 2 + static_cast<int>(mouth_ * 9);
  int mw = 20 + static_cast<int>(mouth_ * 8);
  int my = cy + static_cast<int>(cur_.eyeH / 2) + 4;
  if (my + mh > SCREEN_H - 1) my = SCREEN_H - 1 - mh;
  u8g2_.setDrawColor(1);
  u8g2_.drawRBox(cx - mw / 2, my, mw, mh, min(mh / 2, 4));
  if (mh > 6) {  // a darker inside when it's wide open
    u8g2_.setDrawColor(0);
    u8g2_.drawRBox(cx - mw / 2 + 3, my + 2, mw - 6, mh - 4, 2);
    u8g2_.setDrawColor(1);
  }
}

void Face::drawFlourishes(int leftCx, int rightCx, int cy, int eyeTop, int eyeBottom) {
  u8g2_.setDrawColor(1);
  int half = static_cast<int>(cur_.eyeW / 2);
  uint32_t since = millis() - emotionSinceMs_;

  switch (emotion_) {
    case Emotion::Surprised:
      if (since < 900) {
        u8g2_.setFont(u8g2_font_10x20_tr);
        u8g2_.drawStr(SCREEN_W - 14, 20, "!");
      }
      break;
    case Emotion::Happy:
      if (cur_.lowerLid > 8) {
        // Blush: two short slashes below the outer corner of each eye.
        for (int i = 0; i < 2; ++i) {
          int lx = leftCx - half - 8 + i * 3, rx = rightCx + half + 3 + i * 3;
          u8g2_.drawLine(lx, eyeBottom + 1, lx + 3, eyeBottom - 3);
          u8g2_.drawLine(rx, eyeBottom + 1, rx + 3, eyeBottom - 3);
        }
      }
      break;
    case Emotion::Sad:
      if (tearY_ >= 0) {
        int tx = rightCx + half - 3;
        int ty = eyeBottom + static_cast<int>(tearY_);
        u8g2_.drawDisc(tx, ty, 2);
        u8g2_.drawLine(tx, ty - 5, tx, ty - 2);
      }
      break;
    case Emotion::Thinking:
      for (int i = 0; i < thinkDots_; ++i) {
        u8g2_.drawDisc(SCREEN_W - 26 + i * 8, 9, 2);
      }
      break;
    case Emotion::Angry:
      // Two short "steam" strokes above the outer corners.
      if ((frame_ / 6) % 2 == 0) {
        u8g2_.drawLine(leftCx - half - 4, eyeTop - 4, leftCx - half - 2, eyeTop - 9);
        u8g2_.drawLine(rightCx + half + 4, eyeTop - 4, rightCx + half + 2, eyeTop - 9);
      }
      break;
    default:
      break;
  }
}

void Face::drawZeds() {
  u8g2_.setDrawColor(1);
  for (const Zed& z : zeds_) {
    if (!z.alive) continue;
    if (z.age > 0.8f && (frame_ % 2 == 0)) continue;  // flicker out at the end
    if (z.age < 0.33f) u8g2_.setFont(u8g2_font_6x10_tr);
    else if (z.age < 0.66f) u8g2_.setFont(u8g2_font_9x15B_tr);
    else u8g2_.setFont(u8g2_font_10x20_tr);
    int x = static_cast<int>(z.x), y = static_cast<int>(z.y);
    if (x >= 0 && x < SCREEN_W - 4 && y > 4) u8g2_.drawStr(x, y, "Z");
  }
}

void Face::render() {
  u8g2_.clearBuffer();

  // Vertical bob: breathing while asleep, a bounce with the mouth while talking.
  float bob = asleep_ ? sinf(breath_) * 2.0f : -mouth_ * 2.0f;
  int cy = SCREEN_H / 2 + static_cast<int>(gazeY_ + bob);
  if (talking_) cy -= 2;  // make room for the mouth
  int half = static_cast<int>(cur_.eyeW / 2);
  int leftCx = SCREEN_W / 2 - EYE_GAP / 2 - half + static_cast<int>(gazeX_) + jitterX_;
  int rightCx = SCREEN_W / 2 + EYE_GAP / 2 + half + static_cast<int>(gazeX_) + jitterX_;

  // Height scale: emotion-change overshoot, idle squint, dream twitch.
  float hScale = 1.0f + pop_ * (emotion_ == Emotion::Surprised ? 0.3f : 0.12f);
  hScale *= 1.0f - squint_ * 0.4f;
  if (asleep_) hScale += twitch_ * 2.5f;  // eyelids flutter open a crack

  drawEye(leftCx, cy, true, hScale);
  drawEye(rightCx, cy, false, hScale);

  int eyeTop = cy - static_cast<int>(cur_.eyeH * hScale / 2);
  int eyeBottom = cy + static_cast<int>(cur_.eyeH * hScale / 2);
  if (asleep_) {
    drawZeds();
  } else {
    drawFlourishes(leftCx, rightCx, cy, eyeTop, eyeBottom);
    drawMouth(SCREEN_W / 2 + static_cast<int>(gazeX_ * 0.5f), cy);
  }
  u8g2_.sendBuffer();
}

void Face::step(uint32_t nowMs) {
  lastFrameMs_ = nowMs;
  stepAnimation(nowMs);
}

void Face::update(uint32_t nowMs) {
  step(nowMs);
  render();
}
