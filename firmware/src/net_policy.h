#pragma once

// Pure rules for NVS Wi-Fi profiles. No Arduino, so a host test can compile it.
// Keep in sync with firmware/test/net_policy_host.cpp.

#include <stdint.h>
#include <string.h>

constexpr int NET_MAX_PROFILES = 4;
constexpr int NET_SSID_MAX = 32;
constexpr int NET_PASS_MAX = 64;
constexpr int NET_HOST_MAX = 64;

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

// Direct-join order for one round: last-ok first, then remaining slots.
// Skipped entries already failed this round. -1 means the round is done.
inline int netNextJoin(int n, int lastOk, const bool* skipped) {
  auto usable = [&](int p) {
    return p >= 0 && p < n && (skipped == nullptr || !skipped[p]);
  };
  if (usable(lastOk)) return lastOk;
  for (int p = 0; p < n; p++) {
    if (p == lastOk) continue;
    if (usable(p)) return p;
  }
  return -1;
}

inline int netLastOkAfterJoin(int joined, int lastOk) { return joined >= 0 ? joined : lastOk; }

enum class NetPortalErr : uint8_t { Ok, Ssid, Pass, Host, Full };

inline bool netFieldPrintable(const char* s, int maxLen, bool allowEmpty) {
  if (!s) return allowEmpty;
  int n = 0;
  for (; s[n]; n++) {
    unsigned char c = static_cast<unsigned char>(s[n]);
    if (n >= maxLen) return false;
    if (c < 32 || c == 127) return false;
  }
  return allowEmpty || n > 0;
}

inline bool netHostLooksOk(const char* h) {
  if (!netFieldPrintable(h, NET_HOST_MAX, false)) return false;
  int dots = 0, acc = 0, digits = 0;
  bool ip = true;
  for (const char* p = h; *p; p++) {
    if (*p == '.') {
      if (digits == 0 || acc > 255) {
        ip = false;
        break;
      }
      dots++;
      acc = 0;
      digits = 0;
    } else if (*p >= '0' && *p <= '9') {
      acc = acc * 10 + (*p - '0');
      digits++;
      if (digits > 3 || acc > 255) {
        ip = false;
        break;
      }
    } else {
      ip = false;
      break;
    }
  }
  if (ip && dots == 3 && digits > 0 && acc <= 255) return true;
  bool prevDot = true;
  int label = 0;
  for (const char* p = h; *p; p++) {
    char c = *p;
    if (c == '.') {
      if (label == 0) return false;
      prevDot = true;
      label = 0;
      continue;
    }
    bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              (c == '-' && !prevDot);
    if (!ok) return false;
    prevDot = false;
    label++;
    if (label > 63) return false;
  }
  return label > 0;
}

inline NetPortalErr netValidatePortal(const char* ssid, const char* pass, const char* host) {
  if (!netFieldPrintable(ssid, NET_SSID_MAX, false)) return NetPortalErr::Ssid;
  int plen = pass ? static_cast<int>(strlen(pass)) : 0;
  if (plen > NET_PASS_MAX) return NetPortalErr::Pass;
  if (plen > 0 && plen < 8) return NetPortalErr::Pass;
  if (pass && !netFieldPrintable(pass, NET_PASS_MAX, true)) return NetPortalErr::Pass;
  if (!netHostLooksOk(host)) return NetPortalErr::Host;
  return NetPortalErr::Ok;
}

// Same SSID updates that slot. A new name uses the next empty slot. Never
// steals another profile when all four are filled.
inline int netPlaceProfile(const NetProfileView* profiles, int count, const char* ssid,
                           bool* replacing) {
  if (replacing) *replacing = false;
  if (!profiles || !ssid || !ssid[0] || count < 0) return -1;
  for (int i = 0; i < count && i < NET_MAX_PROFILES; i++) {
    if (strcmp(profiles[i].ssid, ssid) == 0) {
      if (replacing) *replacing = true;
      return i;
    }
  }
  if (count >= NET_MAX_PROFILES) return -1;
  return count;
}

// staUp never opens setup (brain-down is not our concern). Empty NVS or a
// failed portal join do. Saved nets: only after a full join round and
// setupAfterMs.
inline bool netWantSetup(int count, bool staUp, uint32_t downForMs, uint32_t setupAfterMs,
                         bool inSetup, bool portalJoinFailed, bool triedAll = false) {
  if (staUp) return false;
  if (portalJoinFailed) return true;
  if (inSetup) return true;
  if (count <= 0) return true;
  return triedAll && downForMs >= setupAfterMs;
}

// Arduino WiFi.disconnect(true) / softAPdisconnect(true) / mode(WIFI_OFF)
// run esp_wifi_stop + esp_wifi_deinit. The next mode(STA|AP) calls
// esp_wifi_init while the driver is still stopping → ESP_ERR_WIFI_STOP_STATE
// (12308, "netstack cb reg failed"). Keep the driver running.
struct NetIfaceStep {
  bool staDisconnectWifiOff;
  bool apDisconnectWifiOff;
  bool modeOff;
};

inline NetIfaceStep netIfaceEnterSetup() { return {false, false, false}; }
inline NetIfaceStep netIfaceLeaveSetup() { return {false, false, false}; }

inline bool netIfaceKeepsDriver(NetIfaceStep s) {
  return !s.staDisconnectWifiOff && !s.apDisconnectWifiOff && !s.modeOff;
}

struct NetCommitIn {
  int count;
  int lastOk;
  bool replacing;
  int slot;
  bool joinOk;
};

struct NetCommitOut {
  int count;
  int lastOk;
  bool writeNvs;
  bool restoreBackup;
  bool discardNew;
};

// New SSIDs stay in RAM until association + IP. Updates also wait to persist;
// on failure the previous RAM/NVS copy is restored and last-ok is unchanged.
inline NetCommitOut netCommitPortal(NetCommitIn in) {
  NetCommitOut o{};
  o.count = in.count;
  o.lastOk = in.lastOk;
  if (in.joinOk) {
    o.writeNvs = true;
    o.lastOk = in.slot;
    if (!in.replacing && in.slot == in.count && in.count < NET_MAX_PROFILES) o.count = in.count + 1;
    return o;
  }
  if (in.replacing) o.restoreBackup = true;
  else o.discardNew = true;
  return o;
}

inline bool netCanForget(int index, int count, int lastOk) {
  return index >= 0 && index < count && index != lastOk;
}
