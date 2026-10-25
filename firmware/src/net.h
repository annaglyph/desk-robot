#pragma once

#include <Arduino.h>

#include "net_policy.h"

// Station Wi-Fi from NVS profiles (namespace "wifi"). Does not touch "home" or
// "face". Brain down is not a reason to leave the current network or open setup.
//
// Ordinary `pio run -t upload` keeps NVS. A full chip erase wipes profiles;
// the next boot copies secrets.h again only if the migrated flag is gone.
class Net {
 public:
  void begin(const char* seedSsid, const char* seedPass, const char* seedHost, uint16_t seedPort);
  void update(uint32_t nowMs);

  bool staUp() const { return staUp_; }
  bool setupOn() const { return phase_ == Phase::Setup; }
  const char* ssid() const;
  const char* brainHost() const;
  uint16_t brainPort() const { return port_; }
  int lastOk() const { return lastOk_; }
  int count() const { return count_; }
  void printStatus() const;  // SSIDs and hosts, never passwords
  // USB-only: drop a saved slot that is not last-ok. Compacts the list. Never
  // touches namespaces "home" or "face". Not run automatically.
  void forgetSlot(int index);

 private:
  enum class Phase : uint8_t { Idle, Join, Up, Setup };

  void load();
  void saveMeta();
  void saveProfile(int i);
  void migrateIfNeeded(const char* seedSsid, const char* seedPass, const char* seedHost);
  void startJoin(int idx, uint32_t nowMs);
  void joinNext(uint32_t nowMs);
  void onJoinLost(uint32_t nowMs);
  void startSetup();
  void stopSetup();
  void applyPortalSubmit();
  void noteDown(uint32_t nowMs);
  void noteUp();

  NetProfileView views_[NET_MAX_PROFILES]{};
  char pass_[NET_MAX_PROFILES][NET_PASS_MAX + 1]{};
  char host_[NET_MAX_PROFILES][NET_HOST_MAX + 1]{};
  bool skipped_[NET_MAX_PROFILES]{};
  int count_ = 0;
  int lastOk_ = -1;
  int joining_ = -1;
  uint16_t port_ = 8765;
  Phase phase_ = Phase::Idle;
  bool staUp_ = false;
  bool migrated_ = false;
  bool fromPortal_ = false;
  bool replacing_ = false;
  int portalSlot_ = -1;
  char backupPass_[NET_PASS_MAX + 1]{};
  char backupHost_[NET_HOST_MAX + 1]{};
  uint32_t phaseAt_ = 0;
  uint32_t downSince_ = 0;
  bool triedAll_ = false;
};

extern Net net;
