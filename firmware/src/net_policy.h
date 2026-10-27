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
// setupAfterMs. Callers must pass the real station state. A zero downSince
// is "not down", not "down for the whole uptime".
inline bool netWantSetup(int count, bool staUp, uint32_t downForMs, uint32_t setupAfterMs,
                         bool inSetup, bool portalJoinFailed, bool triedAll = false) {
  if (staUp) return false;
  if (portalJoinFailed) return true;
  if (inSetup) return true;
  if (count <= 0) return true;
  return triedAll && downForMs >= setupAfterMs;
}

inline int netMatchSsid(const NetProfileView* profiles, int count, const char* ssid) {
  if (!profiles || !ssid || !ssid[0] || count <= 0) return -1;
  int n = count < NET_MAX_PROFILES ? count : NET_MAX_PROFILES;
  for (int i = 0; i < n; i++) {
    if (strcmp(profiles[i].ssid, ssid) == 0) return i;
  }
  return -1;
}

// SSID wins when it names a saved profile. An empty SSID (the radio has not
// reported one yet) falls back to the attempt still in flight. A reported
// name that is not saved does not guess.
inline int netAdoptSlot(const NetProfileView* profiles, int count, const char* ssid, int pendingSlot) {
  int matched = netMatchSsid(profiles, count, ssid);
  if (matched >= 0) return matched;
  if (ssid && ssid[0]) return -1;
  if (pendingSlot >= 0 && pendingSlot < count && pendingSlot < NET_MAX_PROFILES) return pendingSlot;
  return -1;
}

enum class NetIdleAction : uint8_t { Wait, Adopt, Setup, Retry };

struct NetIdleIn {
  bool associated;
  bool hasIp;
  int adoptSlot;
  int count;
  uint32_t downForMs;
  uint32_t setupAfterMs;
  uint32_t idleForMs;
  uint32_t retryAfterMs;
  bool triedAll;
};

struct NetIdleOut {
  NetIdleAction action;
  int slot;
};

// A live station (associated, with an IP) is adopted and never replaced by
// setup or another begin. Associated without an IP waits. Otherwise the
// usual 60 s / full-round gate, then the retry interval.
inline NetIdleOut netIdleDecision(NetIdleIn in) {
  NetIdleOut o{};
  o.slot = -1;
  if (in.associated && in.hasIp) {
    o.action = NetIdleAction::Adopt;
    o.slot = in.adoptSlot;
    return o;
  }
  if (in.associated) {
    o.action = NetIdleAction::Wait;
    return o;
  }
  if (netWantSetup(in.count, false, in.downForMs, in.setupAfterMs, false, false, in.triedAll)) {
    o.action = NetIdleAction::Setup;
    return o;
  }
  if (in.idleForMs >= in.retryAfterMs) {
    o.action = NetIdleAction::Retry;
    return o;
  }
  o.action = NetIdleAction::Wait;
  return o;
}

inline bool netCanForget(int index, int count, int lastOk) {
  return index >= 0 && index < count && index != lastOk;
}

enum class NetForgetResult : uint8_t { Drop, Missing, Bad, OutOfRange, LastOk };

struct NetForgetDecision {
  NetForgetResult result;
  int slot;
};

// Whole decimal slot only. Letters, signs, trailing junk, and values that
// do not fit in a signed 32-bit int are Bad, never slot 0. Last-ok and
// out-of-range are identified so the caller can refuse without writing NVS.
inline NetForgetDecision netDecideForget(const char* text, int count, int lastOk) {
  NetForgetDecision d{};
  d.result = NetForgetResult::Bad;
  d.slot = -1;
  if (text == nullptr || text[0] == '\0') {
    d.result = NetForgetResult::Missing;
    return d;
  }
  const char* p = text;
  if (*p == '-' || *p == '+') return d;
  unsigned long acc = 0;
  bool any = false;
  bool overflow = false;
  for (; *p; ++p) {
    if (*p < '0' || *p > '9') return d;
    any = true;
    unsigned digit = static_cast<unsigned>(*p - '0');
    if (acc > (2147483647UL - digit) / 10UL) overflow = true;
    else acc = acc * 10UL + digit;
  }
  if (!any || overflow) return d;
  int slot = static_cast<int>(acc);
  d.slot = slot;
  if (count > 0 && slot < count && slot == lastOk) {
    d.result = NetForgetResult::LastOk;
    return d;
  }
  if (!netCanForget(slot, count, lastOk)) {
    d.result = NetForgetResult::OutOfRange;
    return d;
  }
  d.result = NetForgetResult::Drop;
  return d;
}

// Display only. Does not write through src. Ampersand, angles, and both quotes.
// A false return means dst was too small; dst is then empty so a caller cannot
// insert a cut-off entity.
inline bool netHtmlEscape(char* dst, int cap, const char* src) {
  if (!dst || cap <= 0) return false;
  if (!src) src = "";
  int used = 0;
  dst[0] = '\0';
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(src); *p; ++p) {
    const char* rep = nullptr;
    char one[2];
    one[0] = static_cast<char>(*p);
    one[1] = '\0';
    if (*p == '&') rep = "&amp;";
    else if (*p == '<') rep = "&lt;";
    else if (*p == '>') rep = "&gt;";
    else if (*p == '"') rep = "&quot;";
    else if (*p == '\'') rep = "&#39;";
    else rep = one;
    int n = 0;
    while (rep[n]) n++;
    if (used + n >= cap) {
      dst[0] = '\0';
      return false;
    }
    for (int i = 0; i < n; i++) dst[used++] = rep[i];
    dst[used] = '\0';
  }
  return true;
}

// Shown on submit, while the phone is still on Rocky-Setup. Credentials are
// not in NVS yet. There is no second page after the station join starts.
inline const char* netPortalPendingTitle() { return "Connecting\u2026"; }

inline const char* netPortalPendingBody() {
  return "Rocky is testing that network. He will save it only after it connects. "
         "This phone may disconnect from Rocky-Setup during the attempt. "
         "If it does not connect, Rocky-Setup will come back and you can try again. "
         "Networks that already work stay saved.";
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
