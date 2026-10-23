// desk-robot firmware: an expressive face, a turning head, a speaker, a
// microphone, and a camera. Driven over USB serial (115200 baud,
// type `help`) and, when include/secrets.h exists, over WiFi by the brain.

#include <Arduino.h>
#include <U8g2lib.h>
#include <WiFi.h>
#include <Wire.h>

#include "camera.h"
#include "config.h"
#include "face.h"
#include "idle_motion.h"
#include "link.h"
#include "mic.h"
#include "servo_neck.h"
#include "speaker.h"

#if __has_include("secrets.h")
#include "secrets.h"
#define HAVE_BRAIN 1
#else
#define HAVE_BRAIN 0
#endif

// 1.3" SH1106 128x64 OLED over hardware I2C (SDA=D4/GPIO5, SCL=D5/GPIO6).
// R2 = rotated 180 degrees: the OLED is mounted upside down on the head.
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R2, U8X8_PIN_NONE);

Face face(u8g2);
ServoNeck panNeck;
ServoNeck tiltNeck;
IdleMotion idleMotion;
Link brainLink;
Speaker speaker;
Mic mic;
Camera camera;
uint32_t nextTempMs = 0;
volatile bool speakDonePending = false;  // set by the speaker task, sent by the link task

uint32_t lastServoMs = 0;
uint32_t lastAnimMs = 0;
uint32_t lastOledMs = 0;
uint32_t nextStateMs = 0;
bool demoMode = true;  // cycles emotions on its own; off while the brain is connected
bool brainConnected = false;

// Idle motion stays off until `idle on` or `glance on`. It also waits until
// the brain is connected and Rocky is awake. Closing that gate stops a
// gesture where it is; it does not return the head to center.
void applyIdleGate() {
  idleMotion.setGate(brainConnected && !face.asleep());
}
uint32_t nextDemoEmotionMs = 0;
uint8_t demoEmotionIdx = 0;

#if LOOP_TIMING
// One loop stage. avg/max are reported every 2 s, then cleared.
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

static TimingAcc timeLoop, timeServo, timeOled;
static uint32_t nextTimingMs = 0;
static uint32_t lastSlowMs = 0;

static uint32_t sinceUs(uint32_t t0) { return static_cast<uint32_t>(micros() - t0); }

static void noteCommand(const char* name, uint32_t ageMs) {
  Serial.printf("cmd %s @ %lu age %u\n", name, static_cast<unsigned long>(millis()),
                ageMs);
}
#endif

void printHelp() {
  Serial.println(F("desk-robot commands:"));
  Serial.println(F("  emo <name>   neutral|happy|sad|angry|surprised|sleepy|thinking"));
  Serial.println(F("  pan <deg>    turn head, -70..70 (0 = center, negative = left)"));
  Serial.println(F("  tilt <deg>   nod head, -75 (down)..37 (up), 0 = level"));
  Serial.println(F("  center       head to center on both axes"));
  Serial.println(F("  raw pan|tilt <deg>  calibration move that ignores the limits (watch it!)"));
  Serial.println(F("  blink        blink now"));
  Serial.println(F("  sleep on|off eyes shut, breathing, Z's"));
  Serial.println(F("  demo on|off  idle life: auto blinks and emotion changes"));
  Serial.println(F("  glance on|off [id]  same switch as idle on|off; an id replies idle_ack"));
  Serial.println(F("  idle on|off  curious head motion while awake and connected"));
  Serial.println(F("  idle calm|curious|lively   preset only; idle on to run it"));
  Serial.println(F("  idle status"));
  Serial.println(F("  The brain switches idle itself; typing idle on here is for tests only."));
  Serial.println(F("  volume <0-1> speaker volume"));
  Serial.println(F("  beep         play a test tone through the speaker"));
  Serial.println(F("  mic on|off   stream the microphone to the brain"));
  Serial.println(F("  miclevel     print mic level for 3 s (talk to it)"));
  Serial.println(F("  stream on|off [fps]  stream camera JPEGs to the brain"));
  Serial.println(F("  snap         grab one frame and report its size"));
  Serial.println(F("  temp         chip temperature"));
  Serial.println(F("  help         this text"));
}

void handleCommand(String line, uint32_t ageMs = 0) {
  line.trim();
  if (line.isEmpty()) return;

  int space = line.indexOf(' ');
  String cmd = (space < 0) ? line : line.substring(0, space);
  String arg = (space < 0) ? String() : line.substring(space + 1);
  cmd.toLowerCase();
  arg.trim();

  if (cmd == "help") {
    printHelp();
  } else if (cmd == "emo") {
    Emotion e;
    if (emotionFromName(arg.c_str(), e)) {
      face.setEmotion(e);
      Serial.printf("emotion: %s\n", emotionName(e));
#if LOOP_TIMING
      noteCommand("emo", ageMs);
#endif
    } else {
      Serial.println(F("unknown emotion — try: neutral happy sad angry surprised sleepy thinking"));
    }
  } else if (cmd == "pan") {
    panNeck.setTarget(arg.toFloat());
    idleMotion.noteActivity(millis());
    Serial.printf("pan -> %.0f deg\n", panNeck.target());
#if LOOP_TIMING
    noteCommand("pan", ageMs);
#endif
  } else if (cmd == "tilt") {
    tiltNeck.setTarget(arg.toFloat());
    idleMotion.noteActivity(millis());
    Serial.printf("tilt -> %.0f deg\n", tiltNeck.target());
#if LOOP_TIMING
    noteCommand("tilt", ageMs);
#endif
  } else if (cmd == "raw") {
    // raw pan|tilt <deg>: calibration, ignores the limits. Watch the mechanism!
    int sp = arg.indexOf(' ');
    String axis = sp < 0 ? arg : arg.substring(0, sp);
    float deg = sp < 0 ? 0 : arg.substring(sp + 1).toFloat();
    if (axis == "tilt") tiltNeck.setRaw(deg);
    else if (axis == "pan") panNeck.setRaw(deg);
    else { Serial.println(F("usage: raw pan|tilt <deg>")); return; }
    idleMotion.noteActivity(millis());
    Serial.printf("raw %s -> %.0f deg (limits ignored)\n", axis.c_str(), deg);
  } else if (cmd == "center") {
    panNeck.setTarget(0);
    tiltNeck.setTarget(0);
    idleMotion.noteActivity(millis());
    Serial.println(F("head -> center"));
#if LOOP_TIMING
    noteCommand("center", ageMs);
#endif
  } else if (cmd == "blink") {
    face.blink();
  } else if (cmd == "sleep") {
    face.setAsleep(arg == "on");
    applyIdleGate();
    Serial.printf("sleep %s\n", face.asleep() ? "on" : "off");
#if LOOP_TIMING
    noteCommand(face.asleep() ? "sleep" : "wake", ageMs);
#endif
  } else if (cmd == "speak_begin") {
    speaker.beginSpeech(static_cast<size_t>(arg.toInt()));
#if LOOP_TIMING
    noteCommand("speak_begin", ageMs);
#endif
  } else if (cmd == "speak_end") {
    speaker.endSpeech();
#if LOOP_TIMING
    noteCommand("speak_end", ageMs);
#endif
  } else if (cmd == "volume") {
    speaker.setVolume(arg.toFloat());
    Serial.printf("volume -> %.2f\n", arg.toFloat());
  } else if (cmd == "beep") {
    // 0.4 s of 440 Hz so the amp can be checked without the brain.
    static int16_t tone[16000 * 4 / 10];
    for (size_t i = 0; i < sizeof(tone) / sizeof(tone[0]); ++i) {
      tone[i] = static_cast<int16_t>(8000 * sinf(2 * PI * 440 * i / 16000.0f));
    }
    speaker.beginSpeech();
    speaker.feed(reinterpret_cast<uint8_t*>(tone), sizeof(tone));
    speaker.endSpeech();
    Serial.println(F("beep"));
  } else if (cmd == "mic") {
#if MIC_ENABLED
    mic.setStreaming(arg == "on");
    Serial.printf("mic %s\n", mic.streaming() ? "on" : "off");
#else
    Serial.println(F("mic ignored — microphone disabled (MIC_ENABLED=0)"));
#endif
  } else if (cmd == "miclevel") {
#if MIC_ENABLED
    for (int i = 0; i < 12; ++i) {
      delay(250);
      int bars = static_cast<int>(mic.level() * 200);
      Serial.printf("  level %.3f %.*s\n", mic.level(), min(bars, 40), "########################################");
    }
#else
    Serial.println(F("miclevel ignored — microphone disabled (MIC_ENABLED=0)"));
#endif
  } else if (cmd == "stream") {
#if CAMERA_ENABLED
    int sp = arg.indexOf(' ');
    String onoff = sp < 0 ? arg : arg.substring(0, sp);
    float fps = sp < 0 ? 10.0f : arg.substring(sp + 1).toFloat();
    camera.setStreaming(onoff == "on", fps);
    Serial.printf("stream %s @ %.1f fps\n", camera.streaming() ? "on" : "off", fps);
#else
    Serial.println(F("stream ignored — camera disabled (CAMERA_ENABLED=0)"));
#endif
  } else if (cmd == "snap") {
#if CAMERA_ENABLED
    if (!camera.ok()) {
      Serial.println(F("camera not available"));
    } else {
      bool was = camera.streaming();
      camera.setStreaming(true, 10);
      static uint8_t* jpg = static_cast<uint8_t*>(ps_malloc(64 * 1024));
      size_t n = 0;
      for (int i = 0; i < 40 && n == 0 && jpg; ++i) { delay(25); n = camera.takeFrame(jpg, 64 * 1024); }
      camera.setStreaming(was, 10);
      Serial.printf("snap: %u bytes (%s)\n", n, n ? "ok" : "no frame");
    }
#else
    Serial.println(F("snap ignored — camera disabled (CAMERA_ENABLED=0)"));
#endif
  } else if (cmd == "temp") {
    Serial.printf("chip %.1f C\n", temperatureRead());
  } else if (cmd == "glance") {
    // glance on|off [id]. With an id, reply idle_ack after the cancel, so the
    // brain reads the pose the head actually holds.
    int sp = arg.indexOf(' ');
    String onoff = sp < 0 ? arg : arg.substring(0, sp);
    idleMotion.setEnabled(onoff == "on");
    applyIdleGate();
    Serial.printf("glance %s\n", idleMotion.enabled() ? "on" : "off");
#if HAVE_BRAIN
    if (sp >= 0) {
      uint32_t id = static_cast<uint32_t>(strtoul(arg.substring(sp + 1).c_str(), nullptr, 10));
      // current() is where the neck is. target() can still be a tracking or
      // look destination; cancelTrajectory only stops an idle curve.
      Serial.printf("idle_ack id %lu idle %d pan %.1f tilt %.1f (target %.1f/%.1f)\n",
                    static_cast<unsigned long>(id), idleMotion.enabled() ? 1 : 0,
                    panNeck.current(), tiltNeck.current(), panNeck.target(),
                    tiltNeck.target());
      brainLink.sendIdleAck(id, idleMotion.enabled(), panNeck.current(),
                            tiltNeck.current());
    }
#endif
  } else if (cmd == "idle") {
    String which = arg;
    which.toLowerCase();
    if (which == "on") {
      idleMotion.setEnabled(true);
      applyIdleGate();
      Serial.printf("idle on (%s)\n", idleMotion.preset());
      if (!brainConnected) {
        Serial.println(F("waiting for the brain — connect first, or its glance off clears this"));
      } else if (face.asleep()) {
        Serial.println(F("waiting until awake"));
      }
    } else if (which == "off") {
      idleMotion.setEnabled(false);
      applyIdleGate();
      Serial.println(F("idle off"));
    } else if (which == "calm" || which == "curious" || which == "lively") {
      idleMotion.setPreset(which.c_str());
      Serial.printf("idle preset %s — scale %.2f, duration %.2f, hold %lu-%lu ms\n",
                    idleMotion.preset(), idleMotion.amplitude(), idleMotion.durationScale(),
                    static_cast<unsigned long>(idleMotion.pauseMin()),
                    static_cast<unsigned long>(idleMotion.pauseMax()));
      if (!idleMotion.enabled()) Serial.println(F("idle is off"));
    } else if (which == "status") {
      idleMotion.printStatus(brainConnected, face.asleep(), speaker.speaking());
    } else {
      Serial.println(F("idle on|off|calm|curious|lively|status"));
    }
  } else if (cmd == "demo") {
    demoMode = (arg == "on");
    face.setIdle(demoMode);
    Serial.printf("demo %s\n", demoMode ? "on" : "off");
  } else {
    Serial.println(F("unknown command — type `help`"));
  }
}

void setup() {
  Serial.begin(115200);
  randomSeed(esp_random());

  // ESP32Servo wants its LEDC timers claimed up front.
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  face.begin();
  panNeck.begin(PIN_SERVO_PAN, PAN_MIN_DEG, PAN_MAX_DEG, PAN_MAX_SPEED,
                SERVO_RELAX_MS, PAN_TRIM_DEG, PAN_INVERT);
  tiltNeck.begin(PIN_SERVO_TILT, TILT_MIN_DEG, TILT_MAX_DEG, TILT_MAX_SPEED,
                 SERVO_RELAX_MS, TILT_TRIM_DEG, TILT_INVERT);
  idleMotion.begin(&panNeck, &tiltNeck);

  speaker.begin(PIN_I2S_BCLK, PIN_I2S_LRC, PIN_I2S_DIN, SPEAKER_VOLUME,
                []() { speakDonePending = true; });
  mic.begin(PIN_PDM_CLK, PIN_PDM_DATA, MIC_GAIN);
  if (camera.begin()) Serial.println(F("camera: ready"));

  demoMode = true;
  face.setIdle(true);
  applyIdleGate();  // off: idle is disabled, and no brain yet
  nextDemoEmotionMs = millis() + 8000;

  Serial.println(F("\ndesk-robot v0.3.0 — hello!"));
  Serial.printf("diag: CAMERA_ENABLED=%d MIC_ENABLED=%d LOOP_TIMING=%d\n",
                CAMERA_ENABLED, MIC_ENABLED, LOOP_TIMING);
#if LOOP_TIMING
  Serial.println(F("loop line: Arduino loop, servos, OLED, command-queue age."));
  Serial.println(F("net line: link task. wait is the payload read. hwm is stack left, in bytes."));
  Serial.println(F("slow: one Arduino loop over 50ms. net slow: one ws_.loop() over 50ms."));
  nextTimingMs = millis() + 2000;
#endif
  printHelp();

#if HAVE_BRAIN
  brainLink.onAudio([](const uint8_t* pcm, size_t len) { speaker.feed(pcm, len); });
#if MIC_ENABLED
  // PDM capture owns the existing 16-frame queue; the link task is its only
  // WebSocket consumer. The Arduino loop only applies mic on/off commands.
  brainLink.onMic(&mic);
#endif
#if CAMERA_ENABLED
  // The capture task keeps the latest JPEG. The link task sends it after
  // microphone uplink, so the Arduino loop never touches the socket.
  brainLink.onCamera(&camera);
#endif
  brainLink.onSpeech([](bool begin, size_t bytes) {
    if (begin) speaker.beginSpeech(bytes);
    else speaker.endSpeech();
#if LOOP_TIMING
    // Applied on the link task, in order with the audio. "direct" is not a
    // queue age; pan/tilt/emo/sleep print age from the command queue.
    Serial.printf("cmd %s @ %lu direct\n", begin ? "speak_begin" : "speak_end",
                  static_cast<unsigned long>(millis()));
#endif
  });
  brainLink.setIdleHook([]() {
    if (!speakDonePending) return;
    speakDonePending = false;
    if (WiFi.status() != WL_CONNECTED || !brainLink.connected()) return;
    brainLink.sendJson("{\"type\":\"speak_done\"}");
    Serial.printf("speech: %u underruns, prebuffered %u bytes in %u ms, wifi %d dBm\n",
                  speaker.lastUnderruns(), speaker.lastPrebuffered(),
                  speaker.lastPrebufferMs(), WiFi.RSSI());
  });
#ifndef ROBOT_TOKEN
#define ROBOT_TOKEN ""
#endif
  brainLink.begin(WIFI_SSID, WIFI_PASS, BRAIN_HOST, BRAIN_PORT, ROBOT_TOKEN,
             [](const String& cmd) { handleCommand(cmd, brainLink.lastCommandAgeMs()); },
             [](bool connected) {
               // The brain picks emotions while it's connected; the demo
               // cycle takes over again if it goes away. Blinks stay on
               // either way. Idle stays off unless it was turned on, and a
               // disconnect stops a gesture where it is.
               brainConnected = connected;
               demoMode = !connected;
               if (connected) {
                 face.setEmotion(Emotion::Happy);
               }
               applyIdleGate();
             });
#else
  Serial.println(F("no include/secrets.h — USB-only mode (see secrets.h.example)"));
#endif
}

void loop() {
#if LOOP_TIMING
  uint32_t loopStart = micros();
  uint32_t servoUs = 0, oledUs = 0;
#endif

  // Serial console.
  static String lineBuf;
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (!lineBuf.isEmpty()) handleCommand(lineBuf);
      lineBuf = "";
    } else if (lineBuf.length() < 80) {
      lineBuf += c;
    }
  }

  uint32_t now = millis();

#if HAVE_BRAIN
  // Connection side effects and motion commands run here, never inside the
  // WebSocket task. pollState first so a connect's "happy" lands before a
  // command that was already queued.
  brainLink.pollState();
  brainLink.drainCommands();
  if (brainLink.connected() && now >= nextTempMs) {
    nextTempMs = now + 10000;
    brainLink.sendJson(String("{\"type\":\"temp\",\"c\":") + String(temperatureRead(), 1) + "}");
  }
  if (brainLink.connected() && now >= nextStateMs) {
    nextStateMs = now + 5000;
    brainLink.sendState(panNeck.current(), tiltNeck.current(), idleMotion.enabled(),
                        emotionName(face.emotion()));
  }
#endif

  // Demo mode: wander through emotions so a fresh flash shows everything.
  if (demoMode && now >= nextDemoEmotionMs) {
    static const Emotion cycle[] = {
        Emotion::Neutral, Emotion::Happy,     Emotion::Thinking,
        Emotion::Neutral, Emotion::Surprised, Emotion::Sleepy,
    };
    demoEmotionIdx = (demoEmotionIdx + 1) % (sizeof(cycle) / sizeof(cycle[0]));
    face.setEmotion(cycle[demoEmotionIdx]);
    nextDemoEmotionMs = now + random(6000, 12000);
  }

  // Servos run before the OLED transfer, on their own timer. The link task
  // owns the WebSocket read, so this no longer waits on a payload.
  uint32_t tick = millis();
  if (tick - lastServoMs >= SERVO_INTERVAL_MS) {
    lastServoMs = tick;
#if LOOP_TIMING
    uint32_t tServo = micros();
#endif
    // While one axis swings, keep the other powered so it can't sag.
    if (panNeck.moving()) tiltNeck.hold();
    if (tiltNeck.moving()) panNeck.hold();
    panNeck.update(tick);
    tiltNeck.update(tick);
    // After the step, so a new gesture starts on the next tick.
    idleMotion.service(tick, speaker.speaking());
#if LOOP_TIMING
    servoUs = sinceUs(tServo);
    timeServo.add(servoUs);
#endif
  }
  if (tick - lastAnimMs >= FRAME_INTERVAL_MS) {
    lastAnimMs = tick;
    face.setTalking(speaker.speaking(), speaker.level());
    face.step(tick);
  }
  if (tick - lastOledMs >= OLED_INTERVAL_MS) {
    lastOledMs = tick;
#if LOOP_TIMING
    uint32_t tOled = micros();
#endif
    face.render();
#if LOOP_TIMING
    oledUs = sinceUs(tOled);
    timeOled.add(oledUs);
#endif
  }

#if LOOP_TIMING
  uint32_t loopUs = sinceUs(loopStart);
  timeLoop.add(loopUs);
  if (loopUs >= 50000 && millis() - lastSlowMs >= 500) {
    lastSlowMs = millis();
    Serial.printf("slow %ums: servo %u oled %u\n", loopUs / 1000, servoUs / 1000,
                  oledUs / 1000);
  }
  if (millis() >= nextTimingMs) {
    nextTimingMs = millis() + 2000;
    int rssi = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
    Serial.printf(
        "loop ms avg/max: loop %u/%u servo %u/%u oled %u/%u age %u q %d odrop %u rssi %d\n",
        timeLoop.avgMs(), timeLoop.maxMs(), timeServo.avgMs(), timeServo.maxMs(),
        timeOled.avgMs(), timeOled.maxMs(), brainLink.takeCommandAgeMax(),
        brainLink.commandQueued(), brainLink.takeOutboundDrops(), rssi);
    timeLoop.reset();
    timeServo.reset();
    timeOled.reset();
  }
#endif
}
