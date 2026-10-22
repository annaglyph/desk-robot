#pragma once

#include <Arduino.h>

// Camera: the Sense board's OV2640 → a stream of small JPEGs.
//
// A capture task grabs frames at the requested rate and keeps only the
// latest one (copied into PSRAM). loop() picks it up with takeFrame() and
// ships it to the brain, so a slow WiFi moment just skips frames instead
// of piling them up.
//
// CAMERA_ENABLED=0 (see config.h) skips begin(): no sensor clock, no capture
// task, and loop() does not transmit JPEGs. setStreaming() stays off.

class Camera {
 public:
  bool begin();  // false if the camera didn't come up
  bool ok() const { return ok_; }

  void setStreaming(bool on, float fps);
  bool streaming() const { return streaming_; }

  // Copies the newest unsent JPEG into `out` (capacity `cap`); returns its
  // size, or 0 if there's nothing new or it didn't fit.
  size_t takeFrame(uint8_t* out, size_t cap);

  uint32_t framesCaptured() const { return captured_; }

 private:
  static void taskEntry(void* self);
  void task();

  bool ok_ = false;
  volatile bool streaming_ = false;
  volatile uint32_t intervalMs_ = 100;
  uint8_t* latest_ = nullptr;      // PSRAM
  size_t latestCap_ = 0;
  volatile size_t latestLen_ = 0;
  volatile bool fresh_ = false;
  volatile uint32_t captured_ = 0;
  portMUX_TYPE lock_ = portMUX_INITIALIZER_UNLOCKED;
};
