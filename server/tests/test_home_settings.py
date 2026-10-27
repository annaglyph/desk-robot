"""Home Assistant in V2: the dashboard settings, the token file, live updates, and the launcher's routes.

Everything is offline: Home Assistant's replies are faked, settings.json and the token file are temporary files, and
the launcher never contacts a real brain.
"""

import http.client
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from brain import config, home, home_assistant, settings

TOKEN = "eyJhbGciOiJIUzI1NiJ9.test-token-not-real.sig"
URL = "http://homeassistant.local:8123"
LAMP = "switch.office_desk_lamp"


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.settings_path = root / "settings.json"
        self.token_path = root / "home_assistant.token"
        self.patches = [patch.object(settings, "PATH", self.settings_path),
                        patch.object(home, "TOKEN_PATH", self.token_path)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        quiet(home.controller.configure, {}, "")
        self.dir.cleanup()


class SettingsTests(TempFiles):
    def test_defaults_are_off_and_empty(self):
        s = settings.load()["home_assistant"]
        self.assertEqual(s, {"enabled": False, "url": "", "readings": "", "lights": ""})

    def test_a_valid_section_round_trips_with_its_prefixes(self):
        readings = "pool_temperature = number:sensor.pool_temperature\nprinter_status = text:sensor.printer_status"
        lights = "office_desk_lamp = light:" + LAMP
        settings.update({"home_assistant": {"enabled": True, "url": URL, "readings": readings, "lights": lights}})
        s = settings.load()["home_assistant"]
        self.assertEqual(s, {"enabled": True, "url": URL, "readings": readings, "lights": lights})
        self.assertIn("number:", s["readings"])
        self.assertIn("text:", s["readings"])
        self.assertIn("light:", s["lights"])

    def test_bad_values_are_rejected_on_save_and_the_file_is_unchanged(self):
        settings.update({"home_assistant": {"readings": "pool_temperature = sensor.pool"}})
        before = self.settings_path.read_bytes()
        too_many_readings = "\n".join(f"r{i:02d} = sensor.r{i:02d}" for i in range(33))
        too_many_lights = "\n".join(f"l{i} = light:switch.l{i}" for i in range(9))
        cases = (
            ("url", "homeassistant.local:8123"),
            ("url", URL + "/api"),
            ("url", "http://user:secretpw@homeassistant.local:8123"),
            ("readings", too_many_readings),
            ("readings", "Pool = sensor.pool"),
            ("readings", "pool = not an entity"),
            ("readings", "pool = light:switch.pool"),
            ("readings", "pool = sensor.a\npool = sensor.b"),
            ("lights", too_many_lights),
            ("lights", "lamp = light:sensor.pool_temperature"),
            ("lights", "lamp = light:lock.front_door"),
            ("lights", "lamp = " + LAMP),
            ("lights", "lamp = text:" + LAMP),
            ("lights", "lamp = light:switch.a\nlamp = light:switch.b"),
            ("enabled", "yes"),
            ("readings", "x" * 6001),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value[:40]):
                with self.assertRaises(ValueError) as caught:
                    settings.update({"home_assistant": {key: value}})
                self.assertNotIn("secretpw", str(caught.exception))
                self.assertEqual(self.settings_path.read_bytes(), before)
        with self.assertRaises(ValueError):
            settings.update({"home_assistant": {"token": TOKEN}})   # not a setting: no way to put it here
        self.assertNotIn(TOKEN, self.settings_path.read_text())

    def test_a_hand_edited_bad_value_falls_back_to_the_default(self):
        self.settings_path.write_text(json.dumps({"home_assistant": {
            "enabled": True, "url": URL, "lights": "lamp = light:lock.front_door"}}))
        s = settings.load()["home_assistant"]
        self.assertEqual(s["lights"], "")
        self.assertTrue(s["enabled"])

    def test_computer_settings_are_untouched(self):
        settings.update({"home_assistant": {"enabled": True}})
        self.assertEqual(settings.load()["computer"], settings.defaults()["computer"])


class TokenFileTests(TempFiles):
    def test_save_is_owner_only_and_replaces(self):
        self.assertFalse(home.token_set())
        self.assertEqual(home.load_token(), "")
        home.save_token("  " + TOKEN + "\n")
        self.assertEqual(home.load_token(), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_path).st_mode), 0o600)
        home.save_token("second-token")
        self.assertEqual(home.load_token(), "second-token")
        self.assertEqual(stat.S_IMODE(os.stat(self.token_path).st_mode), 0o600)
        self.assertEqual([p.name for p in self.token_path.parent.iterdir()], ["home_assistant.token"])

    def test_clear(self):
        home.save_token(TOKEN)
        self.assertTrue(home.clear_token())
        self.assertFalse(self.token_path.exists())
        self.assertFalse(home.clear_token())
        self.assertFalse(home.token_set())

    def test_bad_tokens_are_refused_without_echoing_them(self):
        for bad in ("", "   ", "two words", "line\nbreak", "x" * 5000, None, 123, "tab\there"):
            with self.subTest(bad=str(bad)[:20]):
                with self.assertRaises(ValueError) as caught:
                    home.save_token(bad)
                if isinstance(bad, str) and bad.strip():
                    self.assertNotIn(bad.strip()[:10], str(caught.exception))
        self.assertFalse(self.token_path.exists())

    def test_the_token_file_is_gitignored(self):
        ignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text()
        self.assertIn("server/home_assistant.token", ignore.splitlines())

    def test_v1_env_keys_are_named_never_valued(self):
        keys = home.ignored_env_keys({"HOME_ASSISTANT_TOKEN": TOKEN, "HOME_ASSISTANT_URL": URL,
                                      "HA_POOL_TEMPERATURE": "sensor.pool", "HA_EMPTY": " ", "LLM_API_KEY": "x"})
        self.assertEqual(keys, ["HA_POOL_TEMPERATURE", "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL"])
        self.assertNotIn(TOKEN, " ".join(keys))


class FakeHA:
    """Home Assistant's REST API, faked: one lamp, one pool sensor."""

    def __init__(self, status=200, error=None):
        self.status, self.error = status, error
        self.lamp = "off"
        self.gets, self.posts = [], []

    def get(self, url, token, timeout, max_body=None):
        self.gets.append((url, token))
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return self.status, b""
        if url.endswith("/api/"):
            return 200, b'{"message": "API running."}'
        if url.endswith("/api/states"):
            return 200, json.dumps([
                {"entity_id": LAMP, "state": self.lamp, "attributes": {"friendly_name": "Office Desk Lamp"}},
                {"entity_id": "light.porch", "state": "on", "attributes": {"friendly_name": "Porch"}},
                {"entity_id": "sensor.pool_temperature", "state": "27.5",
                 "attributes": {"friendly_name": "Pool Temperature", "unit_of_measurement": "°C"}},
                {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
            ]).encode()
        if url.endswith("/api/states/" + LAMP):
            return 200, json.dumps({"state": self.lamp}).encode()
        if url.endswith("/api/states/sensor.pool_temperature"):
            return 200, json.dumps({"state": "27.5", "attributes": {"unit_of_measurement": "°C"}}).encode()
        return 404, b""

    def post(self, url, token, body, timeout):
        self.posts.append((url, token, json.loads(body)))
        if self.status != 200:
            return self.status, b""
        self.lamp = "on" if url.endswith("/turn_on") else "off"
        return 200, b"[]"


class LiveUpdateTests(TempFiles):
    """Settings and token changes reach the running brain through main.apply_settings, with no restart."""

    def setUp(self):
        super().setUp()
        from brain import main
        self.main = main
        self.ha = FakeHA()
        c = home.controller
        self.saved_transport = (c._fetch, c._post, c._pause)
        c._fetch, c._post, c._pause = self.ha.get, self.ha.post, (lambda _s: None)
        self.saved_config = {k: v for k, v in vars(config).items() if k.isupper()}
        self.saved_settings = main.current_settings

    def tearDown(self):
        c = home.controller
        c._fetch, c._post, c._pause = self.saved_transport
        for k, v in self.saved_config.items():
            setattr(config, k, v)
        self.main.current_settings = self.saved_settings
        quiet(self.main.computer.controller.configure, settings.defaults()["computer"])
        super().tearDown()

    def apply(self, **changes):
        settings.update({"home_assistant": changes}) if changes else None
        _, log = quiet(self.main.apply_settings, settings.load())
        return log

    def names(self):
        return [t["function"]["name"] for t in home.tools(home.controller)]

    def call(self, name, args):
        text, image = self.main.ABILITIES[name](args)
        self.assertIsNone(image)
        return json.loads(text)

    def test_switching_on_adding_removing_and_switching_off_apply_at_once(self):
        self.assertEqual(self.names(), [])
        self.assertEqual(self.call("control_light", {"name": "office_desk_lamp", "action": "on"})["error"],
                         "unknown_name")
        home.save_token(TOKEN)
        log = self.apply(enabled=True, url=URL, readings="pool_temperature = number:sensor.pool_temperature",
                         lights="office_desk_lamp = light:" + LAMP)
        self.assertEqual(self.names(), ["get_home_state", "control_light"])
        self.assertEqual(self.call("get_home_state", {"name": "pool_temperature"}),
                         {"ok": True, "name": "pool_temperature", "value": 27.5, "unit": "°C"})
        self.assertEqual(self.call("control_light", {"name": "office_desk_lamp", "action": "on"})["outcome"],
                         "confirmed")
        for text in (log, json.dumps(home.controller.status())):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(URL, text)
            self.assertNotIn(LAMP, text)
            self.assertNotIn("sensor.", text)
        self.assertIn("office_desk_lamp", log)

        # the light removed in the dashboard: gone from the tools and refused, even if history still mentions it
        posts = len(self.ha.posts)
        self.apply(lights="")
        self.assertEqual(self.names(), ["get_home_state"])
        self.assertEqual(self.call("control_light", {"name": "office_desk_lamp", "action": "off"})["error"],
                         "unknown_name")
        self.assertEqual(len(self.ha.posts), posts)

        # switched off: nothing offered, nothing read
        gets = len(self.ha.gets)
        self.apply(enabled=False)
        self.assertEqual(self.names(), [])
        self.assertEqual(self.call("get_home_state", {"name": "pool_temperature"})["error"], "unknown_name")
        self.assertEqual(len(self.ha.gets), gets)
        self.assertEqual(home.controller.status(), {"on": False, "readings": [], "lights": [], "token_set": True})

    def test_replacing_and_clearing_the_token_apply_on_reload(self):
        self.apply(enabled=True, url=URL, readings="pool_temperature = number:sensor.pool_temperature")
        self.assertEqual(self.call("get_home_state", {"name": "pool_temperature"})["error"], "not_configured")
        self.assertEqual(self.ha.gets, [])
        home.save_token(TOKEN)
        self.apply()
        self.assertTrue(self.call("get_home_state", {"name": "pool_temperature"})["ok"])
        self.assertEqual(self.ha.gets[-1][1], TOKEN)
        home.save_token("replacement-token")
        self.apply()
        self.call("get_home_state", {"name": "pool_temperature"})
        self.assertEqual(self.ha.gets[-1][1], "replacement-token")
        home.clear_token()
        log = self.apply()
        self.assertEqual(self.call("get_home_state", {"name": "pool_temperature"})["error"], "not_configured")
        self.assertIn("no token", log)

    def test_auth_failure_and_unreachable_hub_reach_the_model_as_codes(self):
        home.save_token(TOKEN)
        self.apply(enabled=True, url=URL, readings="pool_temperature = number:sensor.pool_temperature",
                   lights="office_desk_lamp = light:" + LAMP)
        self.ha.status = 401
        self.assertEqual(self.call("get_home_state", {"name": "pool_temperature"})["error"], "authentication_failed")
        self.assertEqual(self.call("control_light", {"name": "office_desk_lamp", "action": "on"})["error"],
                         "authentication_failed")
        self.ha.status, self.ha.error = 200, urllib.error.URLError(OSError(f"refused {TOKEN}"))
        result = self.call("get_home_state", {"name": "pool_temperature"})
        self.assertEqual(result["error"], "connection_failed")
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_the_console_status_never_carries_the_token_or_entity_ids(self):
        home.save_token(TOKEN)
        self.apply(enabled=True, url=URL, readings="pool_temperature = number:sensor.pool_temperature",
                   lights="office_desk_lamp = light:" + LAMP)
        status = self.main.console_state()["home"]
        self.assertEqual(status, {"on": True, "readings": ["pool_temperature"], "lights": ["office_desk_lamp"],
                                  "token_set": True})


class LauncherTests(TempFiles):
    """The launcher's Home Assistant routes over real HTTP, with Home Assistant faked."""

    def setUp(self):
        super().setUp()
        import launcher
        self.launcher = launcher
        self.ha = FakeHA()
        self.reloads = []
        for p in (patch.object(home_assistant, "urllib_get", self.ha.get),
                  patch.object(launcher, "reload_brain", lambda: self.reloads.append(1) or False)):
            p.start()
            self.patches.append(p)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), launcher.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, console=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"}
        if console:
            headers["X-Rocky-Console"] = "1"
        if host:
            headers["Host"] = host
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        r = c.getresponse()
        raw = r.read().decode()
        c.close()
        return r.status, raw

    def test_token_write_and_clear_need_the_console_and_never_echo(self):
        status, raw = self.request("POST", "/launcher/home_assistant/token", {"token": TOKEN}, console=False)
        self.assertEqual(status, 403)
        status, raw = self.request("POST", "/launcher/home_assistant/token", {"token": TOKEN}, host="evil.example")
        self.assertEqual(status, 403)
        self.assertFalse(self.token_path.exists())

        status, raw = self.request("POST", "/launcher/home_assistant/token", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "token_set": True, "applied_live": False})
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(home.load_token(), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_path).st_mode), 0o600)
        self.assertEqual(self.reloads, [1])

        status, raw = self.request("GET", "/launcher/settings")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(raw)["home_assistant_token"], True)
        self.assertNotIn(TOKEN, raw)

        status, raw = self.request("POST", "/launcher/home_assistant/token", {"token": "has a space " + TOKEN})
        self.assertEqual(status, 400)
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(home.load_token(), TOKEN)

        status, raw = self.request("POST", "/launcher/home_assistant/token", {"clear": True})
        self.assertEqual(json.loads(raw)["token_set"], False)
        self.assertFalse(self.token_path.exists())
        self.assertIs(json.loads(self.request("GET", "/launcher/settings")[1])["home_assistant_token"], False)

    def test_connection_test_reports_each_outcome_without_the_token(self):
        status, raw = self.request("POST", "/launcher/home_assistant/test", {})
        self.assertEqual(json.loads(raw)["error"], "not_configured")
        self.assertEqual(self.ha.gets, [])
        settings.update({"home_assistant": {"url": URL}})
        home.save_token(TOKEN)
        status, raw = self.request("POST", "/launcher/home_assistant/test", {"url": "http://elsewhere:1"})
        self.assertEqual((status, json.loads(raw)), (200, {"ok": True}))
        self.assertEqual(self.ha.gets[-1], (URL + "/api/", TOKEN))   # the saved address only
        self.ha.status = 401
        result = json.loads(self.request("POST", "/launcher/home_assistant/test", {})[1])
        self.assertEqual(result["error"], "authentication_failed")
        self.assertIn("rejected the token", result["message"])
        self.ha.status, self.ha.error = 200, urllib.error.URLError(OSError(f"no route {TOKEN}"))
        status, raw = self.request("POST", "/launcher/home_assistant/test", {})
        self.assertEqual(json.loads(raw)["error"], "connection_failed")
        self.assertNotIn(TOKEN, raw)
        self.ha.error = TimeoutError()
        self.assertEqual(json.loads(self.request("POST", "/launcher/home_assistant/test", {})[1])["error"], "timeout")
        self.assertEqual(self.request("POST", "/launcher/home_assistant/test", {}, console=False)[0], 403)

    def test_discovery_lists_but_grants_nothing(self):
        settings.update({"home_assistant": {"enabled": True, "url": URL}})
        home.save_token(TOKEN)
        before = self.settings_path.read_bytes()
        status, raw = self.request("POST", "/launcher/home_assistant/entities", {"q": "", "for": "read"})
        rows = json.loads(raw)["entities"]
        self.assertEqual([r["entity_id"] for r in rows],
                         ["light.porch", "lock.front_door", "sensor.pool_temperature", LAMP])
        self.assertEqual({r["entity_id"]: r["controllable"] for r in rows},
                         {"light.porch": True, "lock.front_door": False, "sensor.pool_temperature": False, LAMP: True})
        self.assertNotIn(TOKEN, raw)

        rows = json.loads(self.request("POST", "/launcher/home_assistant/entities", {"for": "control"})[1])["entities"]
        self.assertEqual([r["entity_id"] for r in rows], ["light.porch", LAMP])

        rows = json.loads(self.request("POST", "/launcher/home_assistant/entities", {"q": "desk"})[1])["entities"]
        self.assertEqual([r["entity_id"] for r in rows], [LAMP])

        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(settings.load()["home_assistant"]["readings"], "")
        self.assertEqual(settings.load()["home_assistant"]["lights"], "")
        self.assertEqual(self.ha.posts, [])
        self.assertEqual(self.reloads, [])
        self.assertEqual(self.request("POST", "/launcher/home_assistant/entities", {}, console=False)[0], 403)

        self.ha.status = 403
        result = json.loads(self.request("POST", "/launcher/home_assistant/entities", {})[1])
        self.assertEqual(result["error"], "authentication_failed")

    def test_settings_route_validates_server_side_and_reloads(self):
        status, raw = self.request("POST", "/launcher/settings",
                                   {"changes": {"home_assistant": {"lights": "lamp = light:lock.front_door"}}})
        self.assertEqual(status, 400)
        self.assertIn("light line 1", json.loads(raw)["error"])
        self.assertEqual(self.reloads, [])
        status, raw = self.request("POST", "/launcher/settings",
                                   {"changes": {"home_assistant": {"lights": "lamp = light:" + LAMP}}})
        self.assertEqual(status, 200)
        self.assertEqual(self.reloads, [1])
        self.assertEqual(json.loads(raw)["settings"]["home_assistant"]["lights"], "lamp = light:" + LAMP)

    def test_adding_a_light_with_empty_lists_does_not_submit_a_reading(self):
        """The dashboard's Add requests carry only their chosen list when both start empty."""
        self.assertEqual(settings.load()["home_assistant"]["readings"], "")
        self.assertEqual(settings.load()["home_assistant"]["lights"], "")
        status, raw = self.request("POST", "/launcher/settings", {"changes": {"home_assistant": {
            "lights": "office_desk_lamp = light:" + LAMP}}})
        self.assertEqual(status, 200)
        saved = json.loads(raw)["settings"]["home_assistant"]
        self.assertEqual(saved["readings"], "")
        self.assertEqual(saved["lights"], "office_desk_lamp = light:" + LAMP)
        status, raw = self.request("POST", "/launcher/settings", {"changes": {"home_assistant": {
            "readings": "pool_temperature = number:sensor.pool_temperature"}}})
        self.assertEqual(status, 200)
        saved = json.loads(raw)["settings"]["home_assistant"]
        self.assertEqual(saved["readings"], "pool_temperature = number:sensor.pool_temperature")
        self.assertEqual(saved["lights"], "office_desk_lamp = light:" + LAMP)
        self.assertEqual(self.reloads, [1, 1])

    def test_adding_to_an_existing_configuration_sends_the_typed_name(self):
        """The served page once turned the Add template `${name} = ...` into `$Rocky = ...` (the launcher fills in
        {name} everywhere), so a second line always failed validation. The broken request stays refused."""
        reading = "pool_temperature=number:sensor.pool_temperature_sensor"
        light = "office_desk_lamp=light:" + LAMP
        settings.update({"home_assistant": {"enabled": True, "url": URL, "readings": reading, "lights": light}})
        robot = self.launcher.active_name()

        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("$" + robot + " = ", page)
        self.assertNotIn("${name}", Path(self.launcher.PAGE).read_text())

        for key, line, new in (("lights", light, f"office_desk_lamp_two = light:{LAMP}"),
                               ("readings", reading, "pool_depth = number:sensor.pool_depth")):
            what = "light" if key == "lights" else "reading"
            status, raw = self.request("POST", "/launcher/settings", {"changes": {"home_assistant": {
                key: f"{line}\n${robot} = {new.split(' = ')[1]}"}}})
            self.assertEqual(status, 400)
            self.assertIn(f"{what} line 2", json.loads(raw)["error"])

            status, raw = self.request("POST", "/launcher/settings", {"changes": {"home_assistant": {
                key: f"{line}\n{new}"}}})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["settings"]["home_assistant"][key], f"{line}\n{new}")
        saved = settings.load()["home_assistant"]
        self.assertTrue(saved["readings"].startswith(reading + "\n"))
        self.assertTrue(saved["lights"].startswith(light + "\n"))
        self.assertEqual(saved["url"], URL)


if __name__ == "__main__":
    unittest.main()
