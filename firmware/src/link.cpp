#include "link.h"

#include <ArduinoJson.h>
#include <WiFi.h>

#include "config.h"   // FW_VERSION

void Link::begin(const char* token, CommandHandler onCommand, StateHandler onState) {
  token_ = token;
  onCommand_ = std::move(onCommand);
  onState_ = std::move(onState);
  ws_.onEvent([this](WStype_t type, uint8_t* payload, size_t length) {
    onEvent(type, payload, length);
  });
  ws_.setReconnectInterval(3000);
  ws_.enableHeartbeat(15000, 3000, 2);  // ping every 15 s, drop after 2 misses
}

void Link::setBrain(const char* host, uint16_t port) {
  if (!host || !host[0] || port == 0) return;
  if (socketOn_ && port_ == port && strcmp(host_, host) == 0) return;
  strlcpy(host_, host, sizeof(host_));
  port_ = port;
  socketOn_ = true;
  connected_ = false;
  lowTxPower_ = false;
  lastTxCheckMs_ = millis() - 5000;
  ws_.begin(host_, port_, "/");
}

void Link::wifiDown() {
  if (!socketOn_ && !connected_) return;
  socketOn_ = false;
  host_[0] = 0;
  port_ = 0;
  if (connected_) {
    connected_ = false;
    if (onState_) onState_(false);
  }
  ws_.disconnect();
}

void Link::update(uint32_t nowMs) {
  if (!socketOn_ || WiFi.status() != WL_CONNECTED) return;

  // Transmit power to suit the signal. Sending (camera pictures, all day) is the chip's biggest power draw and so its
  // biggest heat source, and full power is only needed far from the router: 11 dBm while the router comes in loud,
  // full power again once it fades. RSSI is how loud the router is to us, which our own power doesn't change, so
  // this can't feed back on itself; the gap between the two thresholds stops it flapping.
  if (nowMs - lastTxCheckMs_ >= 5000) {
    lastTxCheckMs_ = nowMs;
    int rssi = WiFi.RSSI();
    bool low = lowTxPower_ ? rssi > -62 : rssi >= -50;
    if (rssi != 0 && low != lowTxPower_) {
      lowTxPower_ = low;
      WiFi.setTxPower(low ? WIFI_POWER_11dBm : WIFI_POWER_19_5dBm);
      Serial.printf("wifi: signal %d dBm, transmit power %s\n", rssi, low ? "11 dBm (router close)" : "full");
    }
  }

  // Each ws_.loop() reads at most ONE message. While a reply streams in (ten audio messages a second, plus the
  // brain's heartbeat replies) a single read per pass of loop() falls behind as soon as a pass also sends a camera
  // frame: the audio arrives late (gaps), and the heartbeat reply waits behind seconds of audio until the link
  // gives up on it and drops the connection. So read everything that's waiting, for a few ms at most.
  ws_.loop();
  uint32_t t0 = millis();
  for (int i = 0; connected_ && i < 32 && millis() - t0 < 12; ++i) ws_.loop();
}

void Link::sendJson(const String& json) {
  if (!connected_) return;
  ws_.sendTXT(json.c_str(), json.length());
}

void Link::sendBinary(uint8_t type, const uint8_t* data, size_t len) {
  if (!connected_) return;
  static uint8_t* buf = nullptr;
  static const size_t CAP = 512 * 1024;  // mic frames are ~1 KB, stream JPEGs ~10-20 KB, snapshots up to ~300 KB
  if (buf == nullptr) buf = static_cast<uint8_t*>(ps_malloc(CAP));
  if (buf == nullptr || len > CAP - 1) return;
  buf[0] = type;
  memcpy(buf + 1, data, len);
  ws_.sendBIN(buf, len + 1);
}

void Link::sendState(float panDeg, const char* emotion) {
  if (!connected_) return;
  JsonDocument doc;
  doc["type"] = "state";
  doc["pan"] = panDeg;
  doc["emotion"] = emotion;
  String out;
  serializeJson(doc, out);
  sendJson(out);
}

void Link::onEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      connected_ = true;
      Serial.println(F("brain: connected"));
      JsonDocument doc;
      doc["type"] = "hello";
      doc["who"] = "desk-robot";
      doc["fw"] = FW_VERSION;
      doc["token"] = token_;  // the brain drops connections without it
      String out;
      serializeJson(doc, out);
      sendJson(out);
      if (onState_) onState_(true);
      break;
    }
    case WStype_DISCONNECTED:
      if (connected_) {
        Serial.println(F("brain: disconnected, will retry"));
        if (onState_) onState_(false);
      }
      connected_ = false;
      break;
    case WStype_TEXT: {
      // Copy so we can guarantee a terminator.
      String msg(reinterpret_cast<const char*>(payload), length);
      handleMessage(msg.c_str());
      break;
    }
    case WStype_BIN:
      if (length > 1 && payload[0] == 0x01 && onAudio_) {
        onAudio_(payload + 1, length - 1);
      }
      break;
    default:
      break;
  }
}

void Link::handleMessage(const char* json) {
  JsonDocument doc;
  if (deserializeJson(doc, json)) {
    Serial.printf("brain: bad json: %s\n", json);
    return;
  }
  const char* type = doc["type"] | "";
  String cmd;
  if (!strcmp(type, "emotion")) {
    cmd = String("emo ") + (doc["name"] | "neutral");
  } else if (!strcmp(type, "pan") || !strcmp(type, "tilt")) {
    cmd = String(type) + " " + String(doc["deg"] | 0.0f, 1);
  } else if (!strcmp(type, "speak_begin")) {
    cmd = String("speak_begin ") + String(doc["bytes"] | 0);
  } else if (!strcmp(type, "speak_end")) {
    cmd = type;
  } else if (!strcmp(type, "mouth")) {
    cmd = String("mouth ") + String(doc["level"] | -1.0f, 3);
  } else if (!strcmp(type, "volume")) {
    cmd = String("volume ") + String(doc["level"] | 0.5f, 2);
  } else if (!strcmp(type, "mic")) {
    cmd = String("mic ") + ((doc["on"] | false) ? "on" : "off");
  } else if (!strcmp(type, "glance")) {
    cmd = String("glance ") + ((doc["on"] | true) ? "on" : "off");
  } else if (!strcmp(type, "asleep")) {
    cmd = String("sleep ") + ((doc["on"] | false) ? "on" : "off");
  } else if (!strcmp(type, "settings")) {
    if (onSettings_) onSettings_(json);   // structured: not a console command
    return;
  } else if (!strcmp(type, "face")) {
    if (onFace_) onFace_(json);
    return;
  } else if (!strcmp(type, "motion")) {
    // {"type":"motion","axis":"pan","speed":450,"accel":4000}: the neck's speed limits (dances run it fast)
    cmd = String("motion ") + (doc["axis"] | "pan") + " " + String(doc["speed"] | 0.0f, 0) + " " +
          String(doc["accel"] | 0.0f, 0);
  } else if (!strcmp(type, "snapshot")) {
    cmd = String("snapshot ") + (doc["size"] | "xga");
  } else if (!strcmp(type, "stream")) {
    cmd = String("stream ") + ((doc["on"] | false) ? "on " : "off ") + String(doc["fps"] | 10.0f, 1) + " " +
          (doc["size"] | "qvga");
  } else {
    Serial.printf("brain: unknown message type \"%s\"\n", type);
    return;
  }
  if (onCommand_) onCommand_(cmd);
}
