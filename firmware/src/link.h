#pragma once

#include <Arduino.h>
#include <WebSocketsClient.h>
#include <atomic>
#include <functional>
#include <lwip/sockets.h>

class Mic;
class Camera;

// The library keeps its WiFiClient protected. This exposes only the socket
// descriptor so the link task can test writability before a mic frame.
// The library itself is unchanged.
class LinkWsClient : public WebSocketsClient {
 public:
  int socketFd() const { return _client.tcp != nullptr ? _client.tcp->fd() : -1; }

  // WiFiClient exposes these calls publicly. Read before and after so the log
  // confirms the actual option value on each newly connected socket.
  bool enableTcpNoDelay(int& before, int& after) {
    before = -1;
    after = -1;
    if (_client.tcp == nullptr) return false;
    if (_client.tcp->getOption(TCP_NODELAY, &before) < 0) return false;
    if (_client.tcp->setNoDelay(true) < 0) return false;
    return _client.tcp->getOption(TCP_NODELAY, &after) == 0;
  }
};

// Link: WiFi + a WebSocket to the brain server.
//
// A dedicated FreeRTOS task owns the WebSocket client. Nothing else may call
// into it. Commands for the face and servos are copied onto a bounded queue
// and applied on the Arduino loop. Speaker audio is fed from this task, in
// the same order the server sent it.

class Link {
 public:
  using CommandHandler = std::function<void(const String&)>;
  using StateHandler = std::function<void(bool connected)>;
  using AudioHandler = std::function<void(const uint8_t* pcm, size_t len)>;
  using SpeechHandler = std::function<void(bool begin, size_t bytes)>;
  using IdleHook = void (*)();

  void begin(const char* ssid, const char* pass, const char* host,
             uint16_t port, const char* token, CommandHandler onCommand,
             StateHandler onState = nullptr);

  // Binary frames from the brain: type byte 0x01 = TTS audio (16 kHz s16le).
  // Invoked on the link task.
  void onAudio(AudioHandler h) { onAudio_ = std::move(h); }

  // speak_begin / speak_end, invoked on the link task before later audio
  // frames are fed. Keeps playback start/stop in order with feed().
  void onSpeech(SpeechHandler h) { onSpeech_ = std::move(h); }

  // The link task reads directly from this existing 16-frame PDM queue after
  // it handles incoming WebSocket traffic. The Arduino loop never sends mic
  // frames over the socket.
  void onMic(Mic* mic) { mic_ = mic; }

  // The link task sends at most one JPEG per pass, after microphone uplink
  // and outbound control messages. The Arduino loop never sends camera frames.
  void onCamera(Camera* camera) { camera_ = camera; }

  // Called on the link task after each WebSocket turn. Used for speak_done.
  void setIdleHook(IdleHook hook) { idleHook_ = hook; }

  // Arduino loop. Both return immediately.
  void pollState();
  void drainCommands();

  bool connected() const {
    return stateUp_.load(std::memory_order_acquire);
  }

  // Age of the command currently being applied, in ms. Loop only.
  uint32_t lastCommandAgeMs() const { return lastAgeMs_; }
  // Peak command-queue age since the previous call, then cleared. Loop only.
  uint32_t takeCommandAgeMax();
  int commandQueued() const;
  // Outbound JSON drops since the previous call, then cleared. Loop only.
  uint32_t takeOutboundDrops();

  // Robot → server. From the link task this sends immediately. From any
  // other task it copies the JSON onto a bounded queue and returns.
  void sendJson(const String& json);
  void sendState(float panDeg, float tiltDeg, bool idle, const char* emotion);
  // Reply to a `glance` that carried an id. Pan and tilt are the neck's
  // current angles once that glance has been applied.
  void sendIdleAck(uint32_t id, bool idle, float panDeg, float tiltDeg);
  // Reply to an idle_style command. The numbers are the configuration now
  // in effect. error is set when the command was rejected and nothing changed.
  void sendIdleStyleAck(uint32_t id, bool ok, const char* preset, float scale,
                        float duration, uint32_t pauseMin, uint32_t pauseMax,
                        const char* error = nullptr);
  // Binary frame: one type byte (0x01 = mic audio, 0x02 = JPEG) + payload.
  // Only the link task may call this. Other callers are ignored and return
  // false. The result is the WebSocket library's binary-send result.
  bool sendBinary(uint8_t type, const uint8_t* data, size_t len);

 private:
  static void taskEntry(void* self);
  void task();
  void onEvent(WStype_t type, uint8_t* payload, size_t length);
  void handleMessage(const char* json);
  void publishConnected(bool up);
  void enqueueCommand(const char* text);
  void drainOutbound();
  void drainMic();
  void drainCamera();
  bool socketWritable();
  void noteCmdDrop(const char* text);
  void noteOutDrop();

  LinkWsClient ws_;
  CommandHandler onCommand_;
  StateHandler onState_;
  AudioHandler onAudio_;
  SpeechHandler onSpeech_;
  IdleHook idleHook_ = nullptr;
  Mic* mic_ = nullptr;
  Camera* camera_ = nullptr;
  TaskHandle_t task_ = nullptr;
  QueueHandle_t cmdQ_ = nullptr;
  QueueHandle_t outQ_ = nullptr;
  const char* ssid_ = nullptr;
  const char* pass_ = nullptr;
  const char* host_ = nullptr;
  const char* token_ = "";
  uint16_t port_ = 0;
  std::atomic<bool> stateUp_{false};
  std::atomic<uint32_t> stateSeq_{0};
  uint32_t seenSeq_ = 0;
  bool wifiReported_ = false;
  uint32_t lastAgeMs_ = 0;
  uint32_t cmdAgeMaxMs_ = 0;
  uint32_t cmdDropsWindow_ = 0;
  uint32_t cmdDropsTotal_ = 0;
  uint32_t outDropsWindow_ = 0;
  uint32_t lastCmdDropMs_ = 0;
  uint32_t lastOutDropMs_ = 0;
  uint32_t iterFeedUs_ = 0;
  uint32_t lastNetSlowMs_ = 0;
  uint32_t micSentWindow_ = 0;
  uint32_t micFailuresWindow_ = 0;
  uint32_t micBusyWindow_ = 0;
  uint32_t micMaxSendUs_ = 0;
};
