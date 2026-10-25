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

  bool skip2[2]{};
  int pick = netNextJoin(2, 1, skip2);
  if (pick != 1) fail("two nets: try last-ok (Octopod) first");
  skip2[1] = true;
  pick = netNextJoin(2, 1, skip2);
  if (pick != 0) fail("last-ok down: try Rocky-Test, do not rescan last-ok");
  if (netLastOkAfterJoin(pick, 1) != 0) fail("successful fallback updates last-ok");
  skip2[0] = true;
  if (netNextJoin(2, 1, skip2) != -1) fail("all saved networks unavailable this round");
  if (netNextJoin(2, 1, skip2) == 1) fail("failed net must not be picked again this round");

  bool skip4[4]{};
  pick = netNextJoin(4, 2, skip4);
  if (pick != 2) fail("four profiles start at last-ok");
  skip4[2] = true;
  if (netNextJoin(4, 2, skip4) != 0) fail("four profiles: slot 0 after last-ok");
  skip4[0] = true;
  if (netNextJoin(4, 2, skip4) != 1) fail("four profiles: slot 1 next");
  skip4[1] = true;
  if (netNextJoin(4, 2, skip4) != 3) fail("four profiles: slot 3 last");
  skip4[3] = true;
  if (netNextJoin(4, 2, skip4) != -1) fail("four profiles: round done");

  if (netValidatePortal("", "password1", "192.168.1.1") != NetPortalErr::Ssid)
    fail("empty ssid");
  if (netValidatePortal("Home", "short", "192.168.1.1") != NetPortalErr::Pass)
    fail("short password");
  if (netValidatePortal("Home", "", "192.168.1.1") != NetPortalErr::Ok)
    fail("open network password ok");
  if (netValidatePortal("Home", "password1", "") != NetPortalErr::Host)
    fail("empty host");
  if (netValidatePortal("Home", "password1", "not a host") != NetPortalErr::Host)
    fail("host with spaces");
  if (netValidatePortal("Home", "password1", "192.168.2.1") != NetPortalErr::Ok)
    fail("ipv4 host");
  if (netValidatePortal("Home", "password1", "brain.local") != NetPortalErr::Ok)
    fail("hostname");

  NetProfileView slots[4] = {P("Rocky-Test"), P("Home"), {}, {}};
  bool replacing = false;
  int slot = netPlaceProfile(slots, 2, "Office", &replacing);
  if (slot != 2 || replacing) fail("append new ssid");
  slot = netPlaceProfile(slots, 2, "Rocky-Test", &replacing);
  if (slot != 0 || !replacing) fail("same ssid updates in place");
  NetProfileView full[4] = {P("A"), P("B"), P("C"), P("D")};
  slot = netPlaceProfile(full, 4, "E", &replacing);
  if (slot != -1) fail("full list must not steal a slot");
  slot = netPlaceProfile(full, 4, "C", &replacing);
  if (slot != 2 || !replacing) fail("full list may update an existing ssid");

  if (netWantSetup(2, true, 120000, 60000, false, false, true))
    fail("Wi-Fi up must not open setup when the brain is down");
  if (!netWantSetup(0, false, 0, 60000, false, false)) fail("empty nvs opens setup");
  if (netWantSetup(1, false, 10000, 60000, false, false)) fail("still trying saved nets");
  if (netWantSetup(2, false, 60000, 60000, false, false, false))
    fail("60s without a full round must not open setup");
  if (netWantSetup(2, false, 10000, 60000, false, false, true))
    fail("full round before 60s keeps retrying");
  if (!netWantSetup(2, false, 60000, 60000, false, false, true))
    fail("full round and 60s opens setup");
  if (!netWantSetup(1, false, 0, 60000, false, true)) fail("bad portal join returns to setup");
  if (!netWantSetup(1, false, 0, 60000, true, false)) fail("stay in setup until a join works");

  for (int n = 0; n < 8; n++) {
    if (!netIfaceKeepsDriver(netIfaceEnterSetup())) fail("enter setup must not deinit wifi");
    if (!netIfaceKeepsDriver(netIfaceLeaveSetup())) fail("leave setup must not deinit wifi");
  }
  NetIfaceStep bad{true, false, false};
  if (netIfaceKeepsDriver(bad)) fail("wifiOff on STA disconnect is a 12308 risk");
  bad = {false, true, false};
  if (netIfaceKeepsDriver(bad)) fail("wifiOff on AP disconnect is a 12308 risk");
  bad = {false, false, true};
  if (netIfaceKeepsDriver(bad)) fail("mode OFF deinits the driver");

  NetCommitOut c = netCommitPortal({1, 0, false, 1, false});
  if (!c.discardNew || c.writeNvs || c.count != 1 || c.lastOk != 0)
    fail("failed new network must not consume a slot");
  c = netCommitPortal({1, 0, false, 1, true});
  if (!c.writeNvs || c.count != 2 || c.lastOk != 1 || c.discardNew)
    fail("successful new network persists after IP");
  c = netCommitPortal({1, 0, true, 0, false});
  if (!c.restoreBackup || c.writeNvs || c.lastOk != 0 || c.count != 1)
    fail("failed update rolls back and keeps last-ok");
  c = netCommitPortal({1, 0, true, 0, true});
  if (!c.writeNvs || c.lastOk != 0 || c.restoreBackup) fail("successful update persists");
  c = netCommitPortal({4, 0, false, 4, true});
  if (c.count != 4) fail("cannot grow past four persisted profiles");
  if (netPlaceProfile(full, 4, "E", &replacing) != -1) fail("full store still rejects a fifth ssid");
  if (netCanForget(0, 4, 0)) fail("must not drop last-ok");
  if (!netCanForget(1, 4, 0) || !netCanForget(3, 4, 0)) fail("non last-ok slots may be dropped by hand");

  printf("ok\n");
  return 0;
}
