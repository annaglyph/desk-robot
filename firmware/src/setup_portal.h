#pragma once

#include "net_policy.h"

// Captive portal on Rocky-Setup (Stage 1 proof, now used by production Net).
// Never prints Wi-Fi passwords, SETUP_AP_PASS, or ROBOT_TOKEN.
class SetupPortal {
 public:
  void begin(const char* apPass);
  void update();
  void stop();
  bool running() const { return running_; }

  void setKnownSsids(const NetProfileView* views, int count);
  void setError(NetPortalErr err);
  bool takeSubmit(char* ssid, char* pass, char* host);

  uint8_t stations() const { return stations_; }
  uint32_t hits() const { return hits_; }

 private:
  void attachRoutes();
  void serveForm();
  void serveThanks();
  void serveCaptive();

  bool running_ = false;
  uint8_t stations_ = 0;
  uint8_t lastStations_ = 255;
  uint32_t hits_ = 0;
  NetPortalErr err_ = NetPortalErr::Ok;
  bool haveSubmit_ = false;
  char ssid_[NET_SSID_MAX + 1]{};
  char pass_[NET_PASS_MAX + 1]{};
  char host_[NET_HOST_MAX + 1]{};
  char known_[NET_MAX_PROFILES][NET_SSID_MAX + 1]{};
  int knownCount_ = 0;
};

extern SetupPortal setupPortal;
