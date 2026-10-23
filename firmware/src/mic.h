#pragma once

#include <Arduino.h>
#include <atomic>

// Mic: the Sense board's PDM microphone → 16 kHz mono s16le frames.
//
// A capture task reads the I2S peripheral in 30 ms chunks and drops each one
// into a queue of 16 (~0.5 s). If the queue is full the oldest frame is
// dropped. The dedicated link task pops at most four frames after each
// incoming WebSocket turn and ships them to the brain. The Arduino loop only
// enables or disables streaming; it never calls the WebSocket client.
//
// MIC_ENABLED=0 (see config.h) skips begin(): no PDM clock, no capture
// task, or queued microphone frames.

class Mic {
 public:
  static const size_t FRAME_SAMPLES = 480;  // 30 ms at 16 kHz
  static const size_t FRAME_BYTES = FRAME_SAMPLES * 2;

  void begin(uint8_t clkPin, uint8_t dataPin, float gain);
  void setStreaming(bool on) { streaming_ = on; }
  bool streaming() const { return streaming_; }

  // Pops one captured frame into `out` (FRAME_BYTES). Returns false if none.
  bool nextFrame(uint8_t* out);

  // Frames waiting to be sent. 0 when the capture task was not started.
  int queued() const;

  // Frames discarded because the 16-frame capture queue was full. The window
  // count is read-and-cleared by the link task's two-second diagnostics.
  uint32_t takeDroppedWindow() { return droppedWindow_.exchange(0); }
  uint32_t droppedTotal() const { return droppedTotal_.load(); }

  // RMS (0..1) of the most recent frame, for level checks over serial.
  float level() const { return level_; }

 private:
  static void taskEntry(void* self);
  void task();

  QueueHandle_t queue_ = nullptr;
  volatile bool streaming_ = false;
  volatile float level_ = 0.0f;
  std::atomic<uint32_t> droppedWindow_{0};
  std::atomic<uint32_t> droppedTotal_{0};
  float gain_ = 1.0f;
};
