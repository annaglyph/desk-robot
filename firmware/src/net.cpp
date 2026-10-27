#include "net.h"

#include <Preferences.h>
#include <WiFi.h>

#include "config.h"
#include "setup_portal.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#if !defined(SETUP_AP_PASS) && __has_include("../test/setup_ap/setup_ap_secrets.h")
#include "../test/setup_ap/setup_ap_secrets.h"
#endif

Net net;

static const char* kNs = "wifi";  // not "home" (clock) or "face" (pack)

#if defined(SETUP_AP_PASS)
static_assert(sizeof(SETUP_AP_PASS) > 8, "SETUP_AP_PASS must be at least 8 characters for WPA2");
#endif

const char* Net::ssid() const {
  if (joining_ >= 0 && joining_ < NET_MAX_PROFILES && views_[joining_].ssid[0]) {
    return views_[joining_].ssid;
  }
  if (lastOk_ >= 0 && lastOk_ < count_) return views_[lastOk_].ssid;
  return "";
}

const char* Net::brainHost() const {
  int i = joining_ >= 0 ? joining_ : lastOk_;
  if (i >= 0 && i < NET_MAX_PROFILES && host_[i][0]) return host_[i];
  return "";
}

void Net::printStatus() const {
  Serial.printf("wifi: %d profile(s), last-ok %d, %s", count_, lastOk_,
                phase_ == Phase::Up     ? "up"
                : phase_ == Phase::Join ? "joining"
                : phase_ == Phase::Setup ? "setup"
                                        : "idle");
  if (staUp_) Serial.printf(", ip %s", WiFi.localIP().toString().c_str());
  if (phase_ == Phase::Setup) {
    Serial.printf(", ap Rocky-Setup stations %u hits %lu", setupPortal.stations(),
                  static_cast<unsigned long>(setupPortal.hits()));
  }
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

static void clearSlotRam(NetProfileView* view, char* pass, char* host) {
  view->ssid[0] = 0;
  pass[0] = 0;
  host[0] = 0;
}

void Net::forgetSlot(int index) {
  if (!netCanForget(index, count_, lastOk_)) {
    Serial.printf("wifi: will not drop slot %d (last-ok %d, count %d)\n", index, lastOk_, count_);
    return;
  }
  Serial.printf("wifi: dropping saved \"%s\" (slot %d)\n", views_[index].ssid, index);
  for (int i = index; i < count_ - 1; i++) {
    views_[i] = views_[i + 1];
    strlcpy(pass_[i], pass_[i + 1], sizeof(pass_[i]));
    strlcpy(host_[i], host_[i + 1], sizeof(host_[i]));
    saveProfile(i);
  }
  count_--;
  if (lastOk_ > index) lastOk_--;
  clearSlotRam(&views_[count_], pass_[count_], host_[count_]);
  saveProfile(count_);
  saveMeta();
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

void Net::noteDown(uint32_t nowMs) {
  if (downSince_ == 0) downSince_ = nowMs ? nowMs : 1;
}

void Net::noteUp() { downSince_ = 0; }

bool Net::staLive() const {
  if (WiFi.status() != WL_CONNECTED) return false;
  return static_cast<uint32_t>(WiFi.localIP()) != 0;
}

int Net::liveSlot() const {
  String ssid = WiFi.SSID();
  return netAdoptSlot(views_, count_, ssid.c_str(), pendingSlot_);
}

void Net::adoptStation(int slot) {
  pendingSlot_ = -1;
  joining_ = slot;
  staUp_ = true;
  phase_ = Phase::Up;
  triedAll_ = false;
  memset(skipped_, 0, sizeof(skipped_));
  noteUp();
  fromPortal_ = false;
  replacing_ = false;
  portalSlot_ = -1;
  memset(backupPass_, 0, sizeof(backupPass_));
  if (slot >= 0) {
    int nextOk = netLastOkAfterJoin(slot, lastOk_);
    if (lastOk_ != nextOk) {
      lastOk_ = nextOk;
      saveMeta();
      Serial.printf("wifi: last-ok now slot %d brain %s\n", lastOk_,
                    host_[lastOk_][0] ? host_[lastOk_] : "(none)");
    }
  } else {
    Serial.println(F("wifi: connected, saved profile not identified — previous brain host kept"));
  }
  Serial.printf("wifi: connected, ip %s\n", WiFi.localIP().toString().c_str());
}

void Net::begin(const char* seedSsid, const char* seedPass, const char* seedHost, uint16_t seedPort) {
  port_ = seedPort ? seedPort : 8765;
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  load();
  migrateIfNeeded(seedSsid, seedPass, seedHost);
  Serial.printf("wifi: %d saved network(s)%s\n", count_, migrated_ ? ", nvs migrated" : "");
  uint32_t now = millis();
  noteDown(now);
  memset(skipped_, 0, sizeof(skipped_));
  triedAll_ = false;
  if (netWantSetup(count_, false, 0, WIFI_SETUP_AFTER_MS, false, false, false)) {
    startSetup();
  } else {
    joinNext(now);
  }
}

void Net::startJoin(int idx, uint32_t nowMs) {
  if (idx < 0 || idx >= NET_MAX_PROFILES || !views_[idx].ssid[0]) return;
  pendingSlot_ = idx;
  joining_ = idx;
  phase_ = Phase::Join;
  phaseAt_ = nowMs ? nowMs : 1;
  staUp_ = false;
  Serial.printf("wifi: joining \"%s\" (slot %d)...\n", views_[idx].ssid, idx);
  if (WiFi.getMode() != WIFI_STA) WiFi.mode(WIFI_STA);
  WiFi.disconnect(false, false);
  WiFi.begin(views_[idx].ssid, pass_[idx]);
}

void Net::joinNext(uint32_t nowMs) {
  if (staLive()) {
    adoptStation(liveSlot());
    return;
  }
  if (count_ <= 0) {
    startSetup();
    return;
  }
  int pick = netNextJoin(count_, lastOk_, skipped_);
  if (pick < 0) {
    triedAll_ = true;
    Serial.println(F("wifi: tried every saved network this round"));
    uint32_t downFor = downSince_ == 0 ? 0 : nowMs - downSince_;
    if (netWantSetup(count_, false, downFor, WIFI_SETUP_AFTER_MS, false, false, true)) {
      Serial.println(F("wifi: saved networks not joined — starting setup AP"));
      startSetup();
      return;
    }
    phase_ = Phase::Idle;
    phaseAt_ = nowMs ? nowMs : 1;
    return;
  }
  startJoin(pick, nowMs);
}

void Net::onJoinLost(uint32_t nowMs) {
  Serial.printf("wifi: join failed (status %d)\n", static_cast<int>(WiFi.status()));
  if (fromPortal_) {
    NetCommitOut done = netCommitPortal({count_, lastOk_, replacing_, portalSlot_, false});
    if (done.restoreBackup && portalSlot_ >= 0) {
      strlcpy(pass_[portalSlot_], backupPass_, sizeof(pass_[portalSlot_]));
      strlcpy(host_[portalSlot_], backupHost_, sizeof(host_[portalSlot_]));
      Serial.printf("wifi: restored previous settings for \"%s\"\n", views_[portalSlot_].ssid);
    }
    if (done.discardNew && portalSlot_ >= 0) {
      Serial.printf("wifi: discarded unsaved \"%s\"\n", views_[portalSlot_].ssid);
      clearSlotRam(&views_[portalSlot_], pass_[portalSlot_], host_[portalSlot_]);
    }
    fromPortal_ = false;
    replacing_ = false;
    portalSlot_ = -1;
    memset(backupPass_, 0, sizeof(backupPass_));
    joining_ = -1;
    staUp_ = false;
    startSetup();
    return;
  }
  if (joining_ >= 0 && joining_ < NET_MAX_PROFILES) skipped_[joining_] = true;
  joining_ = -1;
  staUp_ = false;
  noteDown(nowMs);
  joinNext(nowMs);
}

void Net::startSetup() {
  if (staLive()) {
    adoptStation(liveSlot());
    return;
  }
  pendingSlot_ = -1;
  joining_ = -1;
  staUp_ = false;
  fromPortal_ = false;
  phase_ = Phase::Setup;
  phaseAt_ = millis();
  setupPortal.setKnownSsids(views_, count_);
  setupPortal.setError(NetPortalErr::Ok);
#if defined(SETUP_AP_PASS)
  setupPortal.begin(SETUP_AP_PASS);
#else
  setupPortal.begin(nullptr);
#endif
}

void Net::stopSetup() {
  if (setupPortal.running()) setupPortal.stop();
}

void Net::applyPortalSubmit() {
  char ssid[NET_SSID_MAX + 1]{};
  char pass[NET_PASS_MAX + 1]{};
  char host[NET_HOST_MAX + 1]{};
  if (!setupPortal.takeSubmit(ssid, pass, host)) return;

  NetPortalErr err = netValidatePortal(ssid, pass, host);
  if (err != NetPortalErr::Ok) {
    setupPortal.setError(err);
    Serial.println(F("wifi: setup form rejected (invalid fields)"));
    return;
  }
  bool replacing = false;
  int slot = netPlaceProfile(views_, count_, ssid, &replacing);
  if (slot < 0) {
    setupPortal.setError(NetPortalErr::Full);
    Serial.println(F("wifi: four profiles already saved, not overwriting"));
    return;
  }

  replacing_ = replacing;
  portalSlot_ = slot;
  if (replacing) {
    strlcpy(backupPass_, pass_[slot], sizeof(backupPass_));
    strlcpy(backupHost_, host_[slot], sizeof(backupHost_));
  } else {
    memset(backupPass_, 0, sizeof(backupPass_));
    backupHost_[0] = 0;
  }

  strlcpy(views_[slot].ssid, ssid, sizeof(views_[slot].ssid));
  strlcpy(pass_[slot], pass, sizeof(pass_[slot]));
  strlcpy(host_[slot], host, sizeof(host_[slot]));
  memset(pass, 0, sizeof(pass));
  Serial.printf("wifi: trying \"%s\" brain %s (%s, not saved until it joins)\n", ssid, host,
                replacing ? "update" : "new");

  stopSetup();
  fromPortal_ = true;
  memset(skipped_, 0, sizeof(skipped_));
  startJoin(slot, millis());
}

void Net::update(uint32_t nowMs) {
  if (phase_ == Phase::Setup) {
    setupPortal.update();
    applyPortalSubmit();
    return;
  }

  if (count_ == 0) {
    if (staLive()) adoptStation(liveSlot());
    else startSetup();
    return;
  }

  bool up = WiFi.status() == WL_CONNECTED;
  if (up) {
    noteUp();
  } else {
    noteDown(nowMs);
  }

  if (phase_ == Phase::Idle) {
    // A join can be marked failed at 20 s and still associate in this gap.
    // downSince_ is 0 once noteUp() has run, so do not treat that as "down for millis()".
    bool hasIp = up && static_cast<uint32_t>(WiFi.localIP()) != 0;
    uint32_t downFor = (!up && downSince_ != 0) ? nowMs - downSince_ : 0;
    NetIdleIn din{};
    din.associated = up;
    din.hasIp = hasIp;
    din.adoptSlot = up ? liveSlot() : -1;
    din.count = count_;
    din.downForMs = downFor;
    din.setupAfterMs = WIFI_SETUP_AFTER_MS;
    din.idleForMs = phaseAt_ == 0 ? 0 : nowMs - phaseAt_;
    din.retryAfterMs = WIFI_RETRY_MS;
    din.triedAll = triedAll_;
    NetIdleOut dec = netIdleDecision(din);
    if (dec.action == NetIdleAction::Adopt) {
      adoptStation(dec.slot);
      return;
    }
    if (dec.action == NetIdleAction::Setup) {
      Serial.println(F("wifi: saved networks not joined — starting setup AP"));
      startSetup();
      return;
    }
    if (dec.action == NetIdleAction::Retry) {
      memset(skipped_, 0, sizeof(skipped_));
      joinNext(nowMs);
    }
    return;
  }

  if (phase_ == Phase::Join) {
    if (up) {
      staUp_ = true;
      phase_ = Phase::Up;
      if (fromPortal_) {
        NetCommitOut done = netCommitPortal({count_, lastOk_, replacing_, joining_, true});
        if (done.writeNvs && joining_ >= 0) {
          count_ = done.count;
          lastOk_ = done.lastOk;
          saveProfile(joining_);
          saveMeta();
          Serial.printf("wifi: saved \"%s\" in slot %d\n", views_[joining_].ssid, joining_);
        }
      } else {
        int nextOk = netLastOkAfterJoin(joining_, lastOk_);
        if (lastOk_ != nextOk) {
          lastOk_ = nextOk;
          saveMeta();
          Serial.printf("wifi: last-ok now slot %d brain %s\n", lastOk_,
                        host_[lastOk_][0] ? host_[lastOk_] : "(none)");
        }
      }
      fromPortal_ = false;
      replacing_ = false;
      portalSlot_ = -1;
      pendingSlot_ = -1;
      memset(backupPass_, 0, sizeof(backupPass_));
      Serial.printf("wifi: connected, ip %s\n", WiFi.localIP().toString().c_str());
      triedAll_ = false;
      memset(skipped_, 0, sizeof(skipped_));
      return;
    }
    if (nowMs - phaseAt_ >= WIFI_JOIN_MS) onJoinLost(nowMs);
    return;
  }

  if (up) {
    staUp_ = true;
    return;
  }
  if (staUp_) {
    Serial.println(F("wifi: lost"));
    staUp_ = false;
    phaseAt_ = nowMs ? nowMs : 1;
    noteDown(nowMs);
    memset(skipped_, 0, sizeof(skipped_));
    triedAll_ = false;
  }
  if (nowMs - phaseAt_ >= WIFI_RETRY_MS) {
    joining_ = -1;
    joinNext(nowMs);
  }
}
