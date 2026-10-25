#include "setup_portal.h"

#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>

SetupPortal setupPortal;

static const char* kApSsid = "Rocky-Setup";
static const IPAddress kApIp(192, 168, 4, 1);
static const IPAddress kApMask(255, 255, 255, 0);

static DNSServer dns;
static WebServer http(80);
static SetupPortal* g = nullptr;

static const char kFormHead[] PROGMEM = R"html(
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Rocky setup</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1.25rem; max-width: 28rem; }
    label { display: block; margin: 0.75rem 0 0.25rem; }
    input, button { font: inherit; width: 100%; box-sizing: border-box; padding: 0.5rem; }
    .note, .err { font-size: 0.9rem; }
    .err { color: #a40000; }
    .note { color: #444; }
  </style>
</head>
<body>
  <h1>Rocky setup</h1>
)html";

static const char kFormTail[] PROGMEM = R"html(
  <form method="post" action="/">
    <label for="ssid">Wi-Fi name (2.4 GHz)</label>
    <input id="ssid" name="ssid" type="text" maxlength="32" autocomplete="off" required>
    <label for="pass">Wi-Fi password</label>
    <input id="pass" name="pass" type="password" maxlength="64" autocomplete="off">
    <label for="brain">Brain host (IP or name)</label>
    <input id="brain" name="brain" type="text" maxlength="64" placeholder="192.168.1.50" autocomplete="off" required>
    <p><button type="submit">Save and join</button></p>
  </form>
</body>
</html>
)html";

static const char kThanks[] PROGMEM = R"html(
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Rocky setup</title>
</head>
<body>
  <h1>Saved</h1>
  <p>Rocky is trying that network. If it does not join, Rocky-Setup will come back. Earlier networks stay in memory.</p>
</body>
</html>
)html";

void SetupPortal::setKnownSsids(const NetProfileView* views, int count) {
  knownCount_ = 0;
  if (!views || count <= 0) return;
  if (count > NET_MAX_PROFILES) count = NET_MAX_PROFILES;
  for (int i = 0; i < count; i++) {
    strlcpy(known_[i], views[i].ssid, sizeof(known_[i]));
  }
  knownCount_ = count;
}

void SetupPortal::setError(NetPortalErr err) { err_ = err; }

bool SetupPortal::takeSubmit(char* ssid, char* pass, char* host) {
  if (!haveSubmit_ || !ssid || !pass || !host) return false;
  strlcpy(ssid, ssid_, NET_SSID_MAX + 1);
  strlcpy(pass, pass_, NET_PASS_MAX + 1);
  strlcpy(host, host_, NET_HOST_MAX + 1);
  haveSubmit_ = false;
  memset(pass_, 0, sizeof(pass_));
  return true;
}

void SetupPortal::serveCaptive() {
  hits_++;
  http.sendHeader("Location", "http://192.168.4.1/", true);
  http.send(302, "text/plain", "");
}

void SetupPortal::serveForm() {
  hits_++;
  String html;
  html.reserve(1600);
  html += FPSTR(kFormHead);
  if (err_ == NetPortalErr::Ssid) html += F("<p class=\"err\">Need a Wi-Fi name of 1–32 characters.</p>");
  else if (err_ == NetPortalErr::Pass)
    html += F("<p class=\"err\">Password must be empty (open network) or at least 8 characters.</p>");
  else if (err_ == NetPortalErr::Host)
    html += F("<p class=\"err\">Need a brain IP address or hostname.</p>");
  else if (err_ == NetPortalErr::Full)
    html += F("<p class=\"err\">Four networks are already saved. Type an existing name to update it. Nothing was overwritten.</p>");
  html += F("<p class=\"note\">2.4 GHz only. The ESP32 cannot join 5 GHz. Passwords are not shown on serial.</p>");
  if (knownCount_ > 0) {
    html += F("<p class=\"note\">Already saved:");
    for (int i = 0; i < knownCount_; i++) {
      html += ' ';
      html += known_[i];
    }
    html += F("</p>");
  }
  html += FPSTR(kFormTail);
  http.send(200, "text/html", html);
}

void SetupPortal::serveThanks() {
  hits_++;
  String ssid = http.arg("ssid");
  String pass = http.arg("pass");
  String host = http.arg("brain");
  NetPortalErr err = netValidatePortal(ssid.c_str(), pass.c_str(), host.c_str());
  if (err != NetPortalErr::Ok) {
    err_ = err;
    serveForm();
    return;
  }
  strlcpy(ssid_, ssid.c_str(), sizeof(ssid_));
  strlcpy(pass_, pass.c_str(), sizeof(pass_));
  strlcpy(host_, host.c_str(), sizeof(host_));
  haveSubmit_ = true;
  err_ = NetPortalErr::Ok;
  http.send_P(200, "text/html", kThanks);
}

void SetupPortal::attachRoutes() {
  g = this;
  http.on("/", HTTP_GET, []() { g->serveForm(); });
  http.on("/", HTTP_POST, []() { g->serveThanks(); });
  http.on("/hotspot-detect.html", HTTP_GET, []() { g->serveCaptive(); });
  http.on("/library/test/success.html", HTTP_GET, []() { g->serveCaptive(); });
  http.on("/generate_204", HTTP_GET, []() { g->serveCaptive(); });
  http.on("/gen_204", HTTP_GET, []() { g->serveCaptive(); });
  http.on("/ncsi.txt", HTTP_GET, []() { g->serveCaptive(); });
  http.onNotFound([]() { g->serveForm(); });
}

void SetupPortal::begin(const char* apPass) {
  if (running_) return;
  if (!apPass || strlen(apPass) < 8) {
    Serial.println(F("wifi: setup AP needs SETUP_AP_PASS (8+ characters) in secrets.h"));
    return;
  }
  haveSubmit_ = false;
  memset(pass_, 0, sizeof(pass_));
  WiFi.persistent(false);
  // Keep the driver: disconnect(true)/mode(OFF) deinit it and the next
  // mode(STA) logs wifi_init_default 12308 (ESP_ERR_WIFI_STOP_STATE).
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(kApIp, kApIp, kApMask);
  bool ok = WiFi.softAP(kApSsid, apPass, 1, 0, 4);
  dns.start(53, "*", kApIp);
  attachRoutes();
  http.begin();
  running_ = true;
  lastStations_ = 255;
  Serial.printf("wifi: setup AP %s %s — open http://192.168.4.1\n", kApSsid, ok ? "up" : "FAILED");
}

void SetupPortal::update() {
  if (!running_) return;
  dns.processNextRequest();
  http.handleClient();
  stations_ = static_cast<uint8_t>(WiFi.softAPgetStationNum());
  if (stations_ != lastStations_) {
    lastStations_ = stations_;
    Serial.printf("wifi: setup stations %u\n", stations_);
  }
}

void SetupPortal::stop() {
  if (!running_) return;
  http.stop();
  dns.stop();
  WiFi.softAPdisconnect(false);
  WiFi.mode(WIFI_STA);
  running_ = false;
  haveSubmit_ = false;
  memset(pass_, 0, sizeof(pass_));
  Serial.println(F("wifi: setup AP stopped"));
}
