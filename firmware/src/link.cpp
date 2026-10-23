#include "link.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <cstring>
#include <lwip/sockets.h>

#include "camera.h"
#include "config.h"
#include "mic.h"
#include "ws_probe.h"

static const char* FW_VERSION = "0.3.0";

// Control commands are short ("pan -70.0", "emo surprised"). 16 of them is
// several seconds of tracking updates, far more than the loop needs.
static const int CMD_Q = 16;
static const size_t CMD_LEN = 48;
// Temp and state JSON. speak_done is sent directly from this task.
static const int OUT_Q = 8;
static const size_t JSON_LEN = 128;

struct CmdItem {
  uint32_t enqueuedMs;
  char text[CMD_LEN];
};

struct OutItem {
  uint16_t len;
  char json[JSON_LEN];
};

#if LOOP_TIMING
struct TimingAcc {
  uint32_t maxUs = 0;
  uint64_t sumUs = 0;
  uint32_t n = 0;
  void add(uint32_t us) {
    if (us > maxUs) maxUs = us;
    sumUs += us;
    n++;
  }
  uint32_t avgMs() const { return n ? static_cast<uint32_t>((sumUs / n) / 1000) : 0; }
  uint32_t maxMs() const { return maxUs / 1000; }
  void reset() { maxUs = 0; sumUs = 0; n = 0; }
};
#endif

void Link::begin(const char* ssid, const char* pass, const char* host,
                 uint16_t port, const char* token, CommandHandler onCommand,
                 StateHandler onState) {
  ssid_ = ssid;
  pass_ = pass;
  host_ = host;
  port_ = port;
  token_ = token ? token : "";
  onCommand_ = std::move(onCommand);
  onState_ = std::move(onState);

  cmdQ_ = xQueueCreate(CMD_Q, sizeof(CmdItem));
  outQ_ = xQueueCreate(OUT_Q, sizeof(OutItem));
  if (cmdQ_ == nullptr || outQ_ == nullptr) {
    Serial.println(F("link: queue create failed"));
    return;
  }

  // Core 0, beside Wi-Fi. Priority 2: below the speaker task (3) and the
  // Wi-Fi task, above the idle task. The Arduino loop stays on core 1, so a
  // blocking payload read cannot stall servo steps or the OLED.
  if (xTaskCreatePinnedToCore(taskEntry, "link", 8192, this, 2, &task_, 0) != pdPASS) {
    Serial.println(F("link: task create failed"));
    task_ = nullptr;
  }
}

void Link::taskEntry(void* self) { static_cast<Link*>(self)->task(); }

#if LOOP_TIMING
#include <esp_wifi.h>

// WiFi.setSleep(false) asks the Arduino core for WIFI_PS_NONE. The core calls
// esp_wifi_set_ps and does not expose that esp_err_t. Its bool is true only
// when the saved mode changed and esp_wifi_set_ps returned ESP_OK. False means
// the mode was already saved, or the IDF call failed. The query below is the
// mode actually in effect. Both lines are diagnostic and stay off unless
// LOOP_TIMING is set.
static const char* wifiPowerSaveName(wifi_ps_type_t mode) {
  switch (mode) {
    case WIFI_PS_NONE: return "WIFI_PS_NONE";
    case WIFI_PS_MIN_MODEM: return "WIFI_PS_MIN_MODEM";
    case WIFI_PS_MAX_MODEM: return "WIFI_PS_MAX_MODEM";
    default: return "unknown";
  }
}

static void logWifiPowerSave() {
  wifi_ps_type_t mode = WIFI_PS_NONE;
  esp_err_t err = esp_wifi_get_ps(&mode);
  if (err == ESP_OK) {
    Serial.printf("wifi: power save %s (%d)\n", wifiPowerSaveName(mode),
                  static_cast<int>(mode));
  } else {
    Serial.printf("wifi: power save query failed, esp_err %d (%s)\n",
                  static_cast<int>(err), esp_err_to_name(err));
  }
}
#endif

void Link::task() {
  WiFi.mode(WIFI_STA);
#if LOOP_TIMING
  bool sleepOff = WiFi.setSleep(false);  // WIFI_PS_NONE; lower latency; wall power
  Serial.printf("wifi: setSleep(false) returned %s\n", sleepOff ? "true" : "false");
#else
  WiFi.setSleep(false);  // WIFI_PS_NONE; lower latency; wall power
#endif
  WiFi.begin(ssid_, pass_);
  Serial.printf("wifi: joining \"%s\"...\n", ssid_);
  Serial.println(F("link: running on core 0, priority 2"));

  ws_.begin(host_, port_, "/");
  ws_.onEvent([this](WStype_t type, uint8_t* payload, size_t length) {
    onEvent(type, payload, length);
  });
  ws_.setReconnectInterval(3000);
  ws_.enableHeartbeat(15000, 3000, 2);  // ping every 15 s, drop after 2 misses

#if LOOP_TIMING
  TimingAcc timeWs, timeWait, timeTcp, timeCb, timeFeed;
  uint32_t nextNetMs = millis() + 2000;
#endif

  for (;;) {
    bool wifiUp = WiFi.status() == WL_CONNECTED;
    if (wifiUp && !wifiReported_) {
      Serial.printf("wifi: connected, ip %s\n", WiFi.localIP().toString().c_str());
#if LOOP_TIMING
      logWifiPowerSave();
#endif
      wifiReported_ = true;
    } else if (!wifiUp && wifiReported_) {
      Serial.println(F("wifi: lost, retrying"));
      wifiReported_ = false;
    }

    if (wifiUp) {
#if LOOP_TIMING
      wsProbe.reset();
      iterFeedUs_ = 0;
      uint32_t t0 = micros();
#endif
      ws_.loop();
#if LOOP_TIMING
      uint32_t wsUs = static_cast<uint32_t>(micros() - t0);
      timeWs.add(wsUs);
      timeWait.add(wsProbe.waitUs);
      timeTcp.add(wsProbe.tcpUs);
      timeCb.add(wsProbe.cbUs);
      timeFeed.add(iterFeedUs_);
      if (wsUs >= 50000 && millis() - lastNetSlowMs_ >= 500) {
        lastNetSlowMs_ = millis();
        Serial.printf("net slow %ums: wait %u tcp %u cb %u feed %u\n",
                      wsUs / 1000, wsProbe.waitUs / 1000, wsProbe.tcpUs / 1000,
                      wsProbe.cbUs / 1000, iterFeedUs_ / 1000);
      }
#endif
      drainOutbound();
      // Receive before transmit: an incoming speaker frame or control command
      // is never held behind microphone upload from an earlier pass.
      drainMic();
    } else if (outQ_ != nullptr) {
      // Don't keep JSON the loop queued while the socket isn't serviced.
      // A later reconnect would otherwise send stale state and temperature.
      OutItem stale;
      while (xQueueReceive(outQ_, &stale, 0) == pdTRUE) {}
    }

    if (idleHook_) idleHook_();
    // After speak_done and the microphone. One frame, and only when the
    // socket already has room, so a JPEG never starts ahead of audio.
    if (wifiUp) drainCamera();

#if LOOP_TIMING
    if (millis() >= nextNetMs) {
      nextNetMs = millis() + 2000;
      uint32_t hwm = uxTaskGetStackHighWaterMark(nullptr);
      Serial.printf(
          "net ms avg/max: ws %u/%u wait %u/%u tcp %u/%u cb %u/%u feed %u/%u "
          "drop %u mic sent %u fail %u busy %u txmax %ums q %d mdrop %u/%u hwm %u\n",
          timeWs.avgMs(), timeWs.maxMs(), timeWait.avgMs(), timeWait.maxMs(),
          timeTcp.avgMs(), timeTcp.maxMs(), timeCb.avgMs(), timeCb.maxMs(),
          timeFeed.avgMs(), timeFeed.maxMs(), cmdDropsWindow_, micSentWindow_,
          micFailuresWindow_, micBusyWindow_, micMaxSendUs_ / 1000, mic_ ? mic_->queued() : 0,
          mic_ ? mic_->takeDroppedWindow() : 0,
          mic_ ? mic_->droppedTotal() : 0, hwm);
      cmdDropsWindow_ = 0;
      micSentWindow_ = 0;
      micFailuresWindow_ = 0;
      micBusyWindow_ = 0;
      micMaxSendUs_ = 0;
      timeWs.reset();
      timeWait.reset();
      timeTcp.reset();
      timeCb.reset();
      timeFeed.reset();
    }
#endif

    // 1 ms while connected so an idle poll does not spin the core. The
    // Arduino loop can drain a command as soon as it is queued; this delay
    // does not sit in front of it. 50 ms while Wi-Fi is down.
    vTaskDelay(pdMS_TO_TICKS(wifiUp ? 1 : 50));
  }
}

void Link::pollState() {
  uint32_t seq = stateSeq_.load(std::memory_order_acquire);
  if (seq == seenSeq_) return;
  bool up = stateUp_.load(std::memory_order_relaxed);
  if (stateSeq_.load(std::memory_order_acquire) != seq) return;
  seenSeq_ = seq;
  if (onState_) onState_(up);
}

void Link::drainCommands() {
  CmdItem item;
  while (cmdQ_ != nullptr && xQueueReceive(cmdQ_, &item, 0) == pdTRUE) {
    uint32_t age = millis() - item.enqueuedMs;
    lastAgeMs_ = age;
    if (age > cmdAgeMaxMs_) cmdAgeMaxMs_ = age;
    if (onCommand_) onCommand_(String(item.text));
  }
}

uint32_t Link::takeCommandAgeMax() {
  uint32_t age = cmdAgeMaxMs_;
  cmdAgeMaxMs_ = 0;
  return age;
}

int Link::commandQueued() const {
  if (cmdQ_ == nullptr) return 0;
  return static_cast<int>(uxQueueMessagesWaiting(cmdQ_));
}

uint32_t Link::takeOutboundDrops() {
  uint32_t n = outDropsWindow_;
  outDropsWindow_ = 0;
  return n;
}

void Link::publishConnected(bool up) {
  stateUp_.store(up, std::memory_order_relaxed);
  stateSeq_.fetch_add(1, std::memory_order_release);
}

void Link::enqueueCommand(const char* text) {
  CmdItem item{};
  item.enqueuedMs = millis();
  size_t n = strlen(text);
  if (n >= sizeof(item.text)) {
    noteCmdDrop(text);
    return;
  }
  memcpy(item.text, text, n + 1);
  // Never block. A full queue means the Arduino loop is stuck; drop this
  // command and keep reading the socket. Already-queued commands stay in order.
  if (cmdQ_ == nullptr || xQueueSend(cmdQ_, &item, 0) != pdTRUE) noteCmdDrop(text);
}

void Link::noteCmdDrop(const char* text) {
  cmdDropsTotal_++;
  cmdDropsWindow_++;
  uint32_t now = millis();
  if (now - lastCmdDropMs_ < 500) return;
  lastCmdDropMs_ = now;
  Serial.printf("link: command queue full, dropped \"%s\" (total %u)\n",
                text, cmdDropsTotal_);
}

void Link::noteOutDrop() {
  outDropsWindow_++;
  uint32_t now = millis();
  if (now - lastOutDropMs_ < 500) return;
  lastOutDropMs_ = now;
  Serial.printf("link: outbound queue full, dropped a message (window %u)\n",
                outDropsWindow_);
}

void Link::drainOutbound() {
  OutItem item;
  while (outQ_ != nullptr && xQueueReceive(outQ_, &item, 0) == pdTRUE) {
    if (!connected()) continue;
    ws_.sendTXT(item.json, item.len);
  }
}

void Link::drainMic() {
  if (!connected() || mic_ == nullptr || !mic_->streaming()) return;

  // 30 ms frames. Capping a pass at four lets the 16-frame queue catch up
  // after a short stall without turning one link turn into an unbounded send.
  static constexpr int MAX_FRAMES_PER_PASS = 4;
  static uint8_t frame[Mic::FRAME_BYTES];
  for (int i = 0; i < MAX_FRAMES_PER_PASS && mic_->queued() > 0; ++i) {
    // Checked before popping, so an unwritable socket leaves the frame queued.
    if (!socketWritable()) {
      micBusyWindow_++;
      break;
    }
    if (!mic_->nextFrame(frame)) break;
    uint32_t started = micros();
    bool sent = sendBinary(0x01, frame, sizeof(frame));
    uint32_t elapsed = static_cast<uint32_t>(micros() - started);
    if (elapsed > micMaxSendUs_) micMaxSendUs_ = elapsed;
    if (sent) {
      micSentWindow_++;
    } else {
      micFailuresWindow_++;
    }
  }
}

void Link::drainCamera() {
  if (!connected() || camera_ == nullptr || !camera_->streaming()) return;
  // A writable socket has room for another microphone frame, not for a whole
  // JPEG. sendBinary still writes the entire WebSocket frame before it
  // returns: WiFiClient::write waits up to a second per attempt and, while
  // any bytes are accepted, keeps going until the frame is finished. Stopping
  // early would leave a partial frame on the connection. The bound here is
  // one frame per pass, started only after incoming audio, outbound control
  // messages, and the microphone queue have had their turn.
  if (!socketWritable()) return;
  static uint8_t* jpg = static_cast<uint8_t*>(ps_malloc(64 * 1024));
  if (jpg == nullptr) return;
  size_t n = camera_->takeFrame(jpg, 64 * 1024);
  if (n == 0) return;
  sendBinary(0x02, jpg, n);
}

// lwIP marks a TCP socket writable only while more than TCP_SNDLOWAT (2,880
// bytes here) of send buffer is free and the segment queue is below its low
// mark, so a ~969-byte mic frame normally fits in one send(MSG_DONTWAIT). This
// task is the socket's only writer, so free space cannot shrink between this
// check and the send. sendBIN() can still wait inside WiFiClient::write() if
// lwIP returns EAGAIN anyway (for example no pbuf memory): that path retries
// select() with a 1 s timeout, up to 10 times. This lowers the chance of a
// blocking mic send; it does not make sendBIN() non-blocking.
bool Link::socketWritable() {
  int fd = ws_.socketFd();
  if (fd < 0) return false;
  fd_set set;
  FD_ZERO(&set);
  FD_SET(fd, &set);
  struct timeval tv = {0, 0};
  return select(fd + 1, nullptr, &set, nullptr, &tv) > 0 && FD_ISSET(fd, &set);
}

void Link::sendJson(const String& json) {
  if (!connected()) return;
  if (task_ != nullptr && xTaskGetCurrentTaskHandle() == task_) {
    ws_.sendTXT(json.c_str(), json.length());
    return;
  }
  if (json.length() >= JSON_LEN) {
    noteOutDrop();
    return;
  }
  OutItem item{};
  item.len = static_cast<uint16_t>(json.length());
  memcpy(item.json, json.c_str(), item.len);
  item.json[item.len] = '\0';
  if (outQ_ == nullptr || xQueueSend(outQ_, &item, 0) != pdTRUE) noteOutDrop();
}

bool Link::sendBinary(uint8_t type, const uint8_t* data, size_t len) {
  // The client is not safe from the Arduino loop. The link task is the only
  // owner; microphone upload reaches here through drainMic().
  if (task_ == nullptr || xTaskGetCurrentTaskHandle() != task_) {
    static bool warned = false;
    if (!warned) {
      warned = true;
      Serial.println(F("link: sendBinary ignored off the link task"));
    }
    return false;
  }
  if (!connected()) return false;
  static uint8_t* buf = nullptr;
  static const size_t CAP = 64 * 1024;  // mic frames are ~1 KB, JPEGs ~10-20 KB
  if (buf == nullptr) buf = static_cast<uint8_t*>(ps_malloc(CAP));
  if (buf == nullptr || len > CAP - 1) return false;
  buf[0] = type;
  memcpy(buf + 1, data, len);
  return ws_.sendBIN(buf, len + 1);
}

void Link::sendState(float panDeg, float tiltDeg, bool idle, const char* emotion) {
  if (!connected()) return;
  JsonDocument doc;
  doc["type"] = "state";
  doc["pan"] = panDeg;
  doc["tilt"] = tiltDeg;
  doc["idle"] = idle;
  doc["emotion"] = emotion;
  String out;
  serializeJson(doc, out);
  sendJson(out);
}

void Link::sendIdleAck(uint32_t id, bool idle, float panDeg, float tiltDeg) {
  if (!connected()) return;
  JsonDocument doc;
  doc["type"] = "idle_ack";
  doc["id"] = id;
  doc["idle"] = idle;
  doc["pan"] = panDeg;
  doc["tilt"] = tiltDeg;
  String out;
  serializeJson(doc, out);
  sendJson(out);
}

void Link::onEvent(WStype_t type, uint8_t* payload, size_t length) {
#if LOOP_TIMING
  uint32_t cbStart = micros();
#endif
  switch (type) {
    case WStype_CONNECTED: {
      publishConnected(true);
      int noDelayBefore = -1;
      int noDelayAfter = -1;
      bool noDelaySet = ws_.enableTcpNoDelay(noDelayBefore, noDelayAfter);
      Serial.println(F("brain: connected"));
      if (noDelaySet) {
        Serial.printf("brain: TCP_NODELAY %s (raw %d -> %d)\n",
                      noDelayAfter ? "on" : "off", noDelayBefore, noDelayAfter);
      } else {
        Serial.println(F("brain: TCP_NODELAY could not be set"));
      }
      JsonDocument doc;
      doc["type"] = "hello";
      doc["who"] = "desk-robot";
      doc["fw"] = FW_VERSION;
      doc["token"] = token_;  // the brain drops connections without it
      String out;
      serializeJson(doc, out);
      sendJson(out);
      break;
    }
    case WStype_DISCONNECTED:
      if (connected()) Serial.println(F("brain: disconnected, will retry"));
      publishConnected(false);
      break;
    case WStype_TEXT: {
      // Copy so we can guarantee a terminator.
      String msg(reinterpret_cast<const char*>(payload), length);
      handleMessage(msg.c_str());
      break;
    }
    case WStype_BIN:
      if (length > 1 && payload[0] == 0x01 && onAudio_) {
#if LOOP_TIMING
        uint32_t tFeed = micros();
#endif
        onAudio_(payload + 1, length - 1);
#if LOOP_TIMING
        iterFeedUs_ += static_cast<uint32_t>(micros() - tFeed);
#endif
      }
      break;
    default:
      break;
  }
#if LOOP_TIMING
  wsProbe.cbUs += static_cast<uint32_t>(micros() - cbStart);
#endif
}

void Link::handleMessage(const char* json) {
  JsonDocument doc;
  if (deserializeJson(doc, json)) {
    Serial.printf("brain: bad json: %s\n", json);
    return;
  }
  const char* type = doc["type"] | "";

  // Applied here, before this task reads the next frame, so begin/feed/end
  // cannot be reordered by the Arduino loop.
  if (!strcmp(type, "speak_begin")) {
    if (onSpeech_) onSpeech_(true, static_cast<size_t>(doc["bytes"] | 0));
    return;
  }
  if (!strcmp(type, "speak_end")) {
    if (onSpeech_) onSpeech_(false, 0);
    return;
  }

  String cmd;
  if (!strcmp(type, "emotion")) {
    cmd = String("emo ") + (doc["name"] | "neutral");
  } else if (!strcmp(type, "pan") || !strcmp(type, "tilt")) {
    cmd = String(type) + " " + String(doc["deg"] | 0.0f, 1);
  } else if (!strcmp(type, "volume")) {
    cmd = String("volume ") + String(doc["level"] | 0.5f, 2);
  } else if (!strcmp(type, "mic")) {
    cmd = String("mic ") + ((doc["on"] | false) ? "on" : "off");
  } else if (!strcmp(type, "glance")) {
    // The id rides through the command queue, so the reply follows the
    // glance and every command queued before it.
    cmd = String("glance ") + ((doc["on"] | true) ? "on" : "off");
    if (doc["id"].is<uint32_t>()) cmd += " " + String(doc["id"].as<uint32_t>());
  } else if (!strcmp(type, "asleep")) {
    cmd = String("sleep ") + ((doc["on"] | false) ? "on" : "off");
  } else if (!strcmp(type, "stream")) {
    cmd = String("stream ") + ((doc["on"] | false) ? "on " : "off ") +
          String(doc["fps"] | 10.0f, 1);
  } else {
    Serial.printf("brain: unknown message type \"%s\"\n", type);
    return;
  }
  enqueueCommand(cmd.c_str());
}
