#include "camera.h"

#include <esp_camera.h>

#include "config.h"

#if CAMERA_ENABLED
// XIAO ESP32S3 Sense camera wiring (from the Arduino core's camera_pins.h).
static const int PIN_PWDN = -1, PIN_RESET = -1, PIN_XCLK = 10;
static const int PIN_SIOD = 40, PIN_SIOC = 39;
static const int PIN_Y9 = 48, PIN_Y8 = 11, PIN_Y7 = 12, PIN_Y6 = 14;
static const int PIN_Y5 = 16, PIN_Y4 = 18, PIN_Y3 = 17, PIN_Y2 = 15;
static const int PIN_VSYNC = 38, PIN_HREF = 47, PIN_PCLK = 13;

static const size_t LATEST_CAP = 64 * 1024;  // QVGA JPEGs are ~8-20 KB
#endif

bool Camera::begin() {
#if !CAMERA_ENABLED
  // Sensor stays unclocked: no DMA, no JPEG encode, no capture task.
  Serial.println(F("camera: disabled (CAMERA_ENABLED=0)"));
  return false;
#else
  camera_config_t cfg = {};
  cfg.ledc_channel = LEDC_CHANNEL_4;  // 0-3 are taken by the servos
  cfg.ledc_timer = LEDC_TIMER_2;
  cfg.pin_d0 = PIN_Y2;
  cfg.pin_d1 = PIN_Y3;
  cfg.pin_d2 = PIN_Y4;
  cfg.pin_d3 = PIN_Y5;
  cfg.pin_d4 = PIN_Y6;
  cfg.pin_d5 = PIN_Y7;
  cfg.pin_d6 = PIN_Y8;
  cfg.pin_d7 = PIN_Y9;
  cfg.pin_xclk = PIN_XCLK;
  cfg.pin_pclk = PIN_PCLK;
  cfg.pin_vsync = PIN_VSYNC;
  cfg.pin_href = PIN_HREF;
  cfg.pin_sccb_sda = PIN_SIOD;
  cfg.pin_sccb_scl = PIN_SIOC;
  cfg.pin_pwdn = PIN_PWDN;
  cfg.pin_reset = PIN_RESET;
  cfg.xclk_freq_hz = 20000000;
  cfg.pixel_format = PIXFORMAT_JPEG;
  cfg.frame_size = FRAMESIZE_QVGA;  // 320x240: plenty for tracking + a live view
  cfg.jpeg_quality = 12;            // 0-63, lower = better; 12 is ~10-15 KB/frame
  cfg.fb_count = 2;
  cfg.fb_location = CAMERA_FB_IN_PSRAM;
  cfg.grab_mode = CAMERA_GRAB_LATEST;

  if (esp_camera_init(&cfg) != ESP_OK) {
    Serial.println(F("camera: init failed"));
    return false;
  }
  sensor_t* s = esp_camera_sensor_get();
  if (s != nullptr) {
    s->set_vflip(s, CAMERA_VFLIP ? 1 : 0);
    s->set_hmirror(s, CAMERA_HMIRROR ? 1 : 0);
  }

  latest_ = static_cast<uint8_t*>(ps_malloc(LATEST_CAP));
  latestCap_ = latest_ ? LATEST_CAP : 0;
  ok_ = latest_ != nullptr;
  if (ok_) xTaskCreatePinnedToCore(taskEntry, "camera", 4096, this, 1, nullptr, 0);
  return ok_;
#endif
}

void Camera::setStreaming(bool on, float fps) {
#if !CAMERA_ENABLED
  (void)on;
  (void)fps;
  streaming_ = false;
  return;
#else
  if (fps < 0.5f) fps = 0.5f;
  if (fps > 20.0f) fps = 20.0f;
  intervalMs_ = static_cast<uint32_t>(1000.0f / fps);
  streaming_ = on;
#endif
}

size_t Camera::takeFrame(uint8_t* out, size_t cap) {
  size_t n = 0;
  portENTER_CRITICAL(&lock_);
  if (fresh_ && latestLen_ <= cap) {
    n = latestLen_;
    memcpy(out, latest_, n);
    fresh_ = false;
  }
  portEXIT_CRITICAL(&lock_);
  return n;
}

void Camera::taskEntry(void* self) { static_cast<Camera*>(self)->task(); }

void Camera::task() {
  for (;;) {
    if (!streaming_) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    uint32_t t0 = millis();
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb != nullptr) {
      if (fb->len <= latestCap_) {
        portENTER_CRITICAL(&lock_);
        memcpy(latest_, fb->buf, fb->len);
        latestLen_ = fb->len;
        fresh_ = true;
        portEXIT_CRITICAL(&lock_);
        captured_ = captured_ + 1;
      }
      esp_camera_fb_return(fb);
    }
    uint32_t spent = millis() - t0;
    uint32_t interval = intervalMs_;
    vTaskDelay(pdMS_TO_TICKS(spent < interval ? interval - spent : 1));
  }
}
