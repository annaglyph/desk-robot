#pragma once

// Pure rules for NVS Wi-Fi profiles. No Arduino, so a host test can compile it.
// Keep in sync with firmware/test/net_policy_host.cpp.

#include <stdint.h>
#include <string.h>

constexpr int NET_MAX_PROFILES = 4;
constexpr int NET_SSID_MAX = 32;
constexpr int NET_PASS_MAX = 64;
constexpr int NET_HOST_MAX = 64;

struct NetScanHit {
  char ssid[NET_SSID_MAX + 1];
  int rssi;
};

struct NetProfileView {
  char ssid[NET_SSID_MAX + 1];
};

struct NetMigrateIn {
  bool nvsMigrated;
  int nvsCount;
  bool hasSeedSsid;
};

struct NetMigrateOut {
  bool writeSeed;    // copy compile-time SSID into slot 0
  bool setMigrated;  // stamp the flag so later flashes do not copy again
};

// First boot with empty NVS copies secrets.h. Ordinary uploads, and an empty
// list after the flag is set, leave NVS alone (Stage 3 is how you add nets).
inline NetMigrateOut netMigrateDecision(NetMigrateIn in) {
  NetMigrateOut out{};
  if (in.nvsMigrated) return out;
  out.setMigrated = true;
  out.writeSeed = in.hasSeedSsid && in.nvsCount <= 0;
  return out;
}

// Among saved profiles, prefer last_ok if that SSID is in this scan and not
// skipped this round. Otherwise the loudest matching saved SSID. If the scan
// shows none of them (hidden AP, empty scan), try last_ok then any unskipped
// profile so a failed join cannot block the rest of the list forever.
inline int netChooseProfile(const NetProfileView* profiles, int n, int lastOk,
                            const NetScanHit* hits, int nHits, const bool* skipped) {
  auto usable = [&](int p) {
    return p >= 0 && p < n && profiles[p].ssid[0] && (skipped == nullptr || !skipped[p]);
  };
  auto visibleRssi = [&](int p) -> int {
    int best = -127;
    bool seen = false;
    for (int i = 0; i < nHits; i++) {
      if (strcmp(hits[i].ssid, profiles[p].ssid) == 0) {
        if (!seen || hits[i].rssi > best) best = hits[i].rssi;
        seen = true;
      }
    }
    return seen ? best : -1000;
  };

  if (usable(lastOk) && visibleRssi(lastOk) > -1000) return lastOk;

  int best = -1, bestRssi = -1000;
  for (int p = 0; p < n; p++) {
    if (!usable(p)) continue;
    int rssi = visibleRssi(p);
    if (rssi > bestRssi) {
      best = p;
      bestRssi = rssi;
    }
  }
  if (best >= 0) return best;
  if (usable(lastOk)) return lastOk;
  for (int p = 0; p < n; p++) {
    if (usable(p)) return p;
  }
  return -1;
}
