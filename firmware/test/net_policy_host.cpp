// Host test for net_policy.h. Built by server/tests/test_wifi_profiles.py, not by PlatformIO.
#include "../src/net_policy.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fail(const char* msg) {
  fprintf(stderr, "FAIL: %s\n", msg);
  exit(1);
}

static NetProfileView P(const char* ssid) {
  NetProfileView p{};
  strncpy(p.ssid, ssid, NET_SSID_MAX);
  return p;
}

static NetScanHit H(const char* ssid, int rssi) {
  NetScanHit h{};
  strncpy(h.ssid, ssid, NET_SSID_MAX);
  h.rssi = rssi;
  return h;
}

int main() {
  NetMigrateIn in{};
  NetMigrateOut d = netMigrateDecision(in);
  if (d.writeSeed || !d.setMigrated) fail("empty nvs, no seed: stamp migrated, do not invent a profile");

  in = {false, 0, true};
  d = netMigrateDecision(in);
  if (!d.writeSeed || !d.setMigrated) fail("first boot copies secrets.h");

  in = {true, 0, true};
  d = netMigrateDecision(in);
  if (d.writeSeed || d.setMigrated) fail("already migrated: ordinary update must not copy secrets.h again");

  in = {false, 2, true};
  d = netMigrateDecision(in);
  if (d.writeSeed) fail("existing profiles must not be overwritten");
  if (!d.setMigrated) fail("stamp migrated if the flag was missing");

  in = {true, 2, true};
  d = netMigrateDecision(in);
  if (d.writeSeed || d.setMigrated) fail("populated nvs after migration is left alone");

  NetProfileView profiles[] = {P("Hotspot"), P("Home"), P("Office")};
  bool skip[3]{};
  NetScanHit hits[] = {H("Home", -40), H("Office", -50), H("Hotspot", -70)};
  int pick = netChooseProfile(profiles, 3, 0, hits, 3, skip);
  if (pick != 0) fail("last-ok Hotspot is used even when Home is louder");

  pick = netChooseProfile(profiles, 3, 0, hits, 3, skip);
  skip[0] = true;
  pick = netChooseProfile(profiles, 3, 0, hits, 3, skip);
  if (pick != 1) fail("failed last-ok must not block Home");

  skip[0] = skip[1] = skip[2] = true;
  pick = netChooseProfile(profiles, 3, 0, hits, 3, skip);
  if (pick != -1) fail("all skipped: no profile");

  memset(skip, 0, sizeof(skip));
  pick = netChooseProfile(profiles, 3, -1, hits, 3, skip);
  if (pick != 1) fail("no last-ok: loudest saved SSID (Home)");

  NetScanHit none[] = {H("Other", -30)};
  pick = netChooseProfile(profiles, 3, 0, none, 1, skip);
  if (pick != 0) fail("hidden/missing scan still tries last-ok");

  skip[0] = true;
  pick = netChooseProfile(profiles, 3, 0, none, 1, skip);
  if (pick != 1) fail("after last-ok fails, try the next saved profile");

  printf("ok\n");
  return 0;
}
