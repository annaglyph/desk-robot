#include "net.h"

#include <Preferences.h>
#include <WiFi.h>

#include "config.h"

Net net;

static const char* kNs = "wifi";  // not "home" (clock) or "face" (pack)

const char* Net::ssid() const {
  if (joining_ >= 0 && joining_ < count_) return views_[joining_].ssid;
  if (lastOk_ >= 0 && lastOk_ < count_) return views_[lastOk_].ssid;
  return "";
}

const char* Net::brainHost() const {
  int i = joining_ >= 0 ? joining_ : lastOk_;
  if (i >= 0 && i < count_ && host_[i][0]) return host_[i];
  return "";
}

void Net::printStatus() const {
  Serial.printf("wifi: %d profile(s), last-ok %d, %s", count_, lastOk_,
                phase_ == Phase::Up     ? "up"
                : phase_ == Phase::Join ? "joining"
                : phase_ == Phase::Scan ? "scan"
                                        : "idle");
  if (staUp_) Serial.printf(", ip %s", WiFi.localIP().toString().c_str());
  Serial.println();
  for (int i = 0; i < count_; i++) {
    Serial.printf("  %d %s  brain %s%s\n", i, views_[i].ssid, host_[i][0] ? host_[i] : "(none)",
                  i == lastOk_ ? "  (last ok)" : "");
  }
  if (count_ == 0) Serial.println(F("wifi: no saved networks"));
}

void Net::load() {
  Preferences p;
  p.begin(kNs, true);
  migrated_ = p.getBool("migrated", false);
  lastOk_ = p.getChar("last", -1);
  count_ = p.getChar("n", 0);
  if (count_ < 0) count_ = 0;
  if (count_ > NET_MAX_PROFILES) count_ = NET_MAX_PROFILES;
  char sk[] = "s0", pk[] = "p0", hk[] = "h0";
  for (int i = 0; i < NET_MAX_PROFILES; i++) {
    sk[1] = pk[1] = hk[1] = static_cast<char>('0' + i);
    strlcpy(views_[i].ssid, p.getString(sk, "").c_str(), sizeof(views_[i].ssid));
    strlcpy(pass_[i], p.getString(pk, "").c_str(), sizeof(pass_[i]));
    strlcpy(host_[i], p.getString(hk, "").c_str(), sizeof(host_[i]));
  }
  p.end();
  int filled = 0;
  for (int i = 0; i < NET_MAX_PROFILES; i++) {
    if (views_[i].ssid[0]) filled = i + 1;
  }
  if (count_ > filled) count_ = filled;
  if (lastOk_ >= count_) lastOk_ = -1;
}

void Net::saveMeta() {
  Preferences p;
  p.begin(kNs, false);
  p.putBool("migrated", migrated_);
  p.putChar("last", static_cast<int8_t>(lastOk_));
  p.putChar("n", static_cast<int8_t>(count_));
  p.end();
}

void Net::saveProfile(int i) {
  if (i < 0 || i >= NET_MAX_PROFILES) return;
  Preferences p;
  p.begin(kNs, false);
  char sk[] = "s0", pk[] = "p0", hk[] = "h0";
  sk[1] = pk[1] = hk[1] = static_cast<char>('0' + i);
  p.putString(sk, views_[i].ssid);
  p.putString(pk, pass_[i]);
  p.putString(hk, host_[i]);
  p.end();
}

void Net::migrateIfNeeded(const char* seedSsid, const char* seedPass, const char* seedHost) {
  NetMigrateIn in{};
  in.nvsMigrated = migrated_;
  in.nvsCount = count_;
  in.hasSeedSsid = seedSsid && seedSsid[0];
  NetMigrateOut d = netMigrateDecision(in);
  if (d.writeSeed) {
    strlcpy(views_[0].ssid, seedSsid, sizeof(views_[0].ssid));
    strlcpy(pass_[0], seedPass ? seedPass : "", sizeof(pass_[0]));
    strlcpy(host_[0], seedHost ? seedHost : "", sizeof(host_[0]));
    count_ = 1;
    lastOk_ = 0;
    saveProfile(0);
    Serial.printf("wifi: saved \"%s\" from secrets.h into nvs\n", views_[0].ssid);
  }
  if (d.setMigrated) {
    migrated_ = true;
    saveMeta();
  }
}

void Net::begin(const char* seedSsid, const char* seedPass, const char* seedHost, uint16_t seedPort) {
  port_ = seedPort ? seedPort : 8765;
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  load();
  migrateIfNeeded(seedSsid, seedPass, seedHost);
  Serial.printf("wifi: %d saved network(s)%s\n", count_, migrated_ ? ", nvs migrated" : "");
  if (count_ == 0) {
    Serial.println(F("wifi: no saved networks — Stage 3 will add a setup page"));
    phase_ = Phase::Idle;
  } else {
    // Casey's working path: WiFi.begin the known SSID. Scanning is only used
    // later to pick among several saved networks; it is not required to join.
    joinLastSaved(millis());
  }
}

void Net::startScan() {
  joining_ = -1;
  staUp_ = false;
  phase_ = Phase::Scan;
  phaseAt_ = millis();
  // scanNetworks already drops the last result. WIFI_SCAN_FAILED (-2) from
  // scanComplete() also means "not started yet" — do not treat it as done.
  int started = WiFi.scanNetworks(true);
  Serial.printf("wifi: scanning (scanNetworks %d, running=%d failed=%d)\n",
                started, WIFI_SCAN_RUNNING, WIFI_SCAN_FAILED);
}

void Net::onScanDone(uint32_t nowMs) {
  int nHits = WiFi.scanComplete();
  NetScanHit hits[16]{};
  int used = 0;
  if (nHits > 0) {
    for (int i = 0; i < nHits && used < 16; i++) {
      String s = WiFi.SSID(i);
      if (!s.length()) continue;
      strlcpy(hits[used].ssid, s.c_str(), sizeof(hits[used].ssid));
      hits[used].rssi = WiFi.RSSI(i);
      used++;
    }
  }
  WiFi.scanDelete();
  Serial.printf("wifi: scan finished %d ap(s), %d named\n", nHits < 0 ? 0 : nHits, used);
  int pick = netChooseProfile(views_, count_, lastOk_, hits, used, skipped_);
  if (pick < 0) {
    Serial.println(F("wifi: scan listed none of our SSIDs"));
    memset(skipped_, 0, sizeof(skipped_));
    joinLastSaved(nowMs);
    return;
  }
  startJoin(pick, nowMs);
}

void Net::startJoin(int idx, uint32_t nowMs) {
  joining_ = idx;
  phase_ = Phase::Join;
  phaseAt_ = nowMs ? nowMs : 1;
  staUp_ = false;
  Serial.printf("wifi: joining \"%s\"...\n", views_[idx].ssid);
  WiFi.begin(views_[idx].ssid, pass_[idx]);
}

void Net::joinLastSaved(uint32_t nowMs) {
  int idx = lastOk_ >= 0 ? lastOk_ : 0;
  if (idx < 0 || idx >= count_ || !views_[idx].ssid[0]) {
    Serial.println(F("wifi: no last saved SSID to join"));
    phase_ = Phase::Idle;
    phaseAt_ = nowMs ? nowMs : 1;
    return;
  }
  Serial.printf("wifi: fallback to saved SSID (no scan)\n");
  startJoin(idx, nowMs);
}

void Net::onJoinLost(uint32_t nowMs) {
  if (joining_ >= 0 && joining_ < NET_MAX_PROFILES) skipped_[joining_] = true;
  Serial.printf("wifi: join failed (status %d), trying another saved network\n",
                static_cast<int>(WiFi.status()));
  joining_ = -1;
  staUp_ = false;
  if (count_ > 1) {
    startScan();
    return;
  }
  memset(skipped_, 0, sizeof(skipped_));
  phase_ = Phase::Idle;
  phaseAt_ = nowMs ? nowMs : 1;
}

void Net::update(uint32_t nowMs) {
  if (count_ == 0) {
    if (nowMs - idleLogAt_ > 10000) {
      idleLogAt_ = nowMs;
      Serial.println(F("wifi: no saved networks"));
    }
    return;
  }

  if (phase_ == Phase::Idle) {
    if (nowMs - phaseAt_ >= WIFI_RETRY_MS) joinLastSaved(nowMs);
    return;
  }

  if (phase_ == Phase::Scan) {
    int st = WiFi.scanComplete();
    if (st == WIFI_SCAN_RUNNING) return;
    if (st >= 0) {
      onScanDone(nowMs);
      return;
    }
    // WIFI_SCAN_FAILED (-2) is also "scan not triggered" (Arduino WiFiScan.cpp).
    if (nowMs - phaseAt_ < WIFI_JOIN_MS) {
      if (nowMs - idleLogAt_ >= 1000) {
        idleLogAt_ = nowMs;
        Serial.printf("wifi: scan wait status %d (running=%d failed=%d) sta %d mode %d\n",
                      st, WIFI_SCAN_RUNNING, WIFI_SCAN_FAILED,
                      static_cast<int>(WiFi.status()), static_cast<int>(WiFi.getMode()));
      }
      return;
    }
    Serial.printf("wifi: scan timed out status %d sta %d mode %d\n",
                  st, static_cast<int>(WiFi.status()), static_cast<int>(WiFi.getMode()));
    WiFi.scanDelete();
    joinLastSaved(nowMs);
    return;
  }

  bool up = WiFi.status() == WL_CONNECTED;
  if (phase_ == Phase::Join) {
    if (up) {
      staUp_ = true;
      phase_ = Phase::Up;
      if (lastOk_ != joining_) {
        lastOk_ = joining_;
        saveMeta();
      }
      Serial.printf("wifi: connected, ip %s\n", WiFi.localIP().toString().c_str());
      memset(skipped_, 0, sizeof(skipped_));
      return;
    }
    if (nowMs - phaseAt_ >= WIFI_JOIN_MS) onJoinLost(nowMs);
    return;
  }

  // Up: a short drop is left to the driver; a long one scans again so another
  // saved network can win. Brain disconnect is not handled here.
  if (up) {
    staUp_ = true;
    return;
  }
  if (staUp_) {
    Serial.println(F("wifi: lost"));
    staUp_ = false;
    phaseAt_ = nowMs ? nowMs : 1;
  }
  if (nowMs - phaseAt_ >= WIFI_RETRY_MS) {
    joining_ = -1;
    if (count_ > 1) startScan();
    else joinLastSaved(nowMs);
  }
}
