#pragma once

#include <Arduino.h>
#include <WebSocketsClient.h>
#include <functional>

#include "net_policy.h"

// Link: a WebSocket to the brain once station Wi-Fi is up (Net joins the AP).
//
// Connects to ws://<host>:<port>, reconnects on its own, and turns the
// server's JSON commands (docs/protocol.md) into the same text commands the
// serial console uses — so "emotion happy" from the brain and "emo happy"
// typed over USB take the exact same path.

class Link {
 public:
  using CommandHandler = std::function<void(const String&)>;
  using StateHandler = std::function<void(bool connected)>;
  using AudioHandler = std::function<void(const uint8_t* pcm, size_t len)>;
  using SettingsHandler = std::function<void(const char* json)>;

  void begin(const char* token, CommandHandler onCommand, StateHandler onState = nullptr);
  // Host/port from the current NVS profile. Restarts the socket if they change.
  void setBrain(const char* host, uint16_t port);
  void wifiDown();

  // Binary frames from the brain: type byte 0x01 = TTS audio (16 kHz s16le).
  void onAudio(AudioHandler h) { onAudio_ = std::move(h); }
  // The brain's `settings` message (clock / weather / screen), passed on as the raw JSON.
  void onSettings(SettingsHandler h) { onSettings_ = std::move(h); }
  // The brain's `face` message (a face pack), passed on as the raw JSON.
  void onFace(SettingsHandler h) { onFace_ = std::move(h); }

  // Call every loop() while station Wi-Fi is up.
  void update(uint32_t nowMs);

  bool connected() const { return connected_; }

  // Robot → server. Text is sent as-is (should be JSON).
  void sendJson(const String& json);
  void sendState(float panDeg, const char* emotion);
  // Binary frame: one type byte (0x01 = mic audio, 0x02 = stream JPEG, 0x03 = snapshot JPEG) + payload.
  void sendBinary(uint8_t type, const uint8_t* data, size_t len);

 private:
  void onEvent(WStype_t type, uint8_t* payload, size_t length);
  void handleMessage(const char* json);

  WebSocketsClient ws_;
  CommandHandler onCommand_;
  StateHandler onState_;
  AudioHandler onAudio_;
  SettingsHandler onSettings_;
  SettingsHandler onFace_;
  const char* token_ = "";
  char host_[NET_HOST_MAX + 1]{};
  uint16_t port_ = 0;
  bool socketOn_ = false;
  bool connected_ = false;
  uint32_t lastTxCheckMs_ = 0;
  bool lowTxPower_ = false;
};
