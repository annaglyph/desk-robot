"""The Home Assistant adapter returns a structured result for every lookup.

HTTP is mocked. These tests do not contact Home Assistant and do not read
HOME_ASSISTANT_TOKEN from the environment.
"""

import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from brain import home_assistant, thinking
from brain.home_assistant import (
    ERRORS,
    MAX_READINGS,
    HomeAssistant,
    Reading,
    readings_from_env,
    urllib_get,
)

TOKEN = "test-token"
URL = "http://homeassistant.local:8123"

_BRAIN = Path(__file__).resolve().parents[1] / "brain"


def ha_body(state, unit="°C") -> bytes:
    attributes = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return json.dumps({
        "entity_id": "sensor.pool_temperature",
        "state": state,
        "attributes": attributes,
    }).encode()


class FakeFetch:
    def __init__(self, status=200, body=b"", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.calls = []

    def __call__(self, url, token, timeout):
        self.calls.append((url, token, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def client(fetch, **entities) -> HomeAssistant:
    catalog = {
        "pool_temperature": Reading("sensor.pool_temperature", True),
        "office_temperature": Reading("sensor.office_temperature", True),
        "printer_progress": Reading("sensor.printer_progress", True),
        "printer_nozzle_temp": Reading("sensor.printer_nozzle_temperature", True),
        "printer_bed_temp": Reading("sensor.printer_bed_temperature", True),
    }
    catalog.update(entities)
    return HomeAssistant(URL, TOKEN, catalog, fetch=fetch)


def assert_failure(test, result, error, name="pool_temperature"):
    test.assertIsInstance(result, dict)
    test.assertEqual(result, {"ok": False, "name": name, "error": error})
    test.assertIn(error, ERRORS)
    test.assertNotIn(TOKEN, json.dumps(result))


class ContractTests(unittest.TestCase):
    def test_valid_numeric_state(self):
        fetch = FakeFetch(200, ha_body("21.4", "°C"))
        result = client(fetch).get_state("pool_temperature")
        self.assertEqual(result, {
            "ok": True,
            "name": "pool_temperature",
            "value": 21.4,
            "unit": "°C",
        })
        self.assertIsInstance(result["value"], float)
        self.assertEqual(
            fetch.calls,
            [(f"{URL}/api/states/sensor.pool_temperature", TOKEN, home_assistant.TIMEOUT)],
        )
        self.assertNotIn(TOKEN, fetch.calls[0][0])

    def test_whole_number_stays_an_integer_and_keeps_its_unit(self):
        fetch = FakeFetch(200, ha_body("63", "%"))
        result = client(fetch).get_state("printer_progress")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["value"], 63)
        self.assertIsInstance(result["value"], int)
        self.assertEqual(result["unit"], "%")

    def test_zero_is_a_real_reading(self):
        result = client(FakeFetch(200, ha_body("0", "°C"))).get_state("printer_bed_temp")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["value"], 0)

    def test_valid_non_numeric_state(self):
        fetch = FakeFetch(200, ha_body("idle", None))
        ha = client(fetch, office_temperature=Reading("sensor.office_status", numeric=False))
        result = ha.get_state("office_temperature")
        self.assertEqual(result, {
            "ok": True,
            "name": "office_temperature",
            "value": "idle",
            "unit": None,
        })
        self.assertEqual(set(result), {"ok", "name", "value", "unit"})

    def test_missing_unit_is_explicitly_null(self):
        result = client(FakeFetch(200, ha_body("23.1", None))).get_state("office_temperature")
        self.assertEqual(result["ok"], True)
        self.assertIsNone(result["unit"])

    def test_does_not_infer_anything_beyond_the_state(self):
        result = client(FakeFetch(200, ha_body("55", "°C"))).get_state("printer_bed_temp")
        self.assertEqual(set(result), {"ok", "name", "value", "unit"})
        self.assertNotIn("printing", json.dumps(result).casefold())

    def test_embedded_unit_text_is_not_parsed_as_a_number(self):
        result = client(FakeFetch(200, ha_body("21.4 °C"))).get_state("pool_temperature")
        assert_failure(self, result, "invalid_state")

    def test_unknown_semantic_name_does_not_call_home_assistant(self):
        fetch = FakeFetch(200, ha_body("1"))
        for name in ("light.living_room", "switch.pool_heater", "automation.whatever", ""):
            result = client(fetch).get_state(name)
            assert_failure(self, result, "unknown_name", name)
        self.assertEqual(fetch.calls, [])

    def test_non_string_name_is_unknown(self):
        result = client(FakeFetch()).get_state(None)
        self.assertEqual(result, {"ok": False, "name": "", "error": "unknown_name"})

    def test_configured_name_with_no_entity_mapping(self):
        fetch = FakeFetch(200, ha_body("21.4"))
        ha = client(fetch, pool_temperature=Reading("", True))
        assert_failure(self, ha.get_state("pool_temperature"), "entity_not_configured")
        assert_failure(
            self,
            client(fetch, pool_temperature=Reading("not an entity", True)).get_state("pool_temperature"),
            "entity_not_configured",
        )
        slipped = "sensor.pool/../api/services/light/turn_on"
        assert_failure(
            self,
            client(fetch, pool_temperature=Reading(slipped, True)).get_state("pool_temperature"),
            "entity_not_configured",
        )
        self.assertEqual(fetch.calls, [])

    def test_semantic_name_is_not_used_as_the_entity_id(self):
        fetch = FakeFetch(200, ha_body("23.1", "°C"))
        ha = client(fetch, office_temperature=Reading("sensor.actual_office", True))
        result = ha.get_state("office_temperature")
        self.assertEqual(result["name"], "office_temperature")
        self.assertEqual(result["value"], 23.1)
        self.assertTrue(fetch.calls[0][0].endswith("/api/states/sensor.actual_office"))
        self.assertNotIn("office_temperature", fetch.calls[0][0])

    def test_http_401_and_403(self):
        assert_failure(self, client(FakeFetch(401, b"no")).get_state("pool_temperature"), "authentication_failed")
        assert_failure(self, client(FakeFetch(403, b"no")).get_state("pool_temperature"), "authentication_failed")

    def test_http_404(self):
        assert_failure(self, client(FakeFetch(404, b"missing")).get_state("pool_temperature"), "entity_not_found")

    def test_other_http_status(self):
        assert_failure(self, client(FakeFetch(500, b"oops")).get_state("pool_temperature"), "request_failed")

    def test_connection_failure_hides_the_exception_text(self):
        fetch = FakeFetch(error=urllib.error.URLError(OSError(f"refused {TOKEN}")))
        result = client(fetch).get_state("pool_temperature")
        assert_failure(self, result, "connection_failed")
        self.assertNotIn("refused", json.dumps(result))

    def test_timeout(self):
        assert_failure(
            self,
            client(FakeFetch(error=TimeoutError("slow"))).get_state("pool_temperature"),
            "timeout",
        )
        wrapped = urllib.error.URLError(TimeoutError("slow"))
        assert_failure(self, client(FakeFetch(error=wrapped)).get_state("pool_temperature"), "timeout")

    def test_ha_state_unknown_unavailable_and_blank(self):
        assert_failure(self, client(FakeFetch(200, ha_body("unknown"))).get_state("pool_temperature"), "invalid_state")
        assert_failure(self, client(FakeFetch(200, ha_body("UNKNOWN"))).get_state("pool_temperature"), "invalid_state")
        assert_failure(
            self,
            client(FakeFetch(200, ha_body("unavailable"))).get_state("pool_temperature"),
            "entity_unavailable",
        )
        assert_failure(
            self,
            client(FakeFetch(200, ha_body(" Unavailable "))).get_state("pool_temperature"),
            "entity_unavailable",
        )
        assert_failure(self, client(FakeFetch(200, ha_body("   "))).get_state("pool_temperature"), "invalid_state")
        assert_failure(self, client(FakeFetch(200, ha_body(""))).get_state("pool_temperature"), "invalid_state")

    def test_malformed_json_and_unexpected_shape(self):
        for body in (b"", b"{", b"not json", b"null", b"[]", b'"21.4"', b"{\"attributes\": {}}"):
            result = client(FakeFetch(200, body)).get_state("pool_temperature")
            assert_failure(self, result, "invalid_state")
        numeric = json.dumps({"state": 21.4, "attributes": {}}).encode()
        assert_failure(self, client(FakeFetch(200, numeric)).get_state("pool_temperature"), "invalid_state")

    def test_numeric_sensor_rejects_non_numeric_garbage(self):
        for state in ("hot", "n/a", "21,4", "0x10", "1e2", "nan", "inf", "on"):
            result = client(FakeFetch(200, ha_body(state))).get_state("pool_temperature")
            assert_failure(self, result, "invalid_state")

    def test_failed_lookup_never_returns_a_blank_result(self):
        """An empty Home Assistant body must not become an empty tool result.

        The mock experiment showed that a blank tool result made the model
        invent a measurement. Every failure here is an explicit object.
        """
        cases = [
            client(FakeFetch(200, b"")).get_state("pool_temperature"),
            client(FakeFetch(200, b"{}")).get_state("pool_temperature"),
            client(FakeFetch(200, ha_body(""))).get_state("pool_temperature"),
            client(FakeFetch(200, ha_body("unknown"))).get_state("pool_temperature"),
            client(FakeFetch(200, ha_body("unavailable"))).get_state("pool_temperature"),
            client(FakeFetch(200, ha_body("hot"))).get_state("pool_temperature"),
            client(FakeFetch(401, b"")).get_state("pool_temperature"),
            client(FakeFetch(403, b"")).get_state("pool_temperature"),
            client(FakeFetch(404, b"")).get_state("pool_temperature"),
            client(FakeFetch(error=TimeoutError())).get_state("pool_temperature"),
            client(FakeFetch(error=urllib.error.URLError("down"))).get_state("pool_temperature"),
            client(FakeFetch(), pool_temperature=Reading("", True)).get_state("pool_temperature"),
            HomeAssistant("", "", {}, fetch=FakeFetch()).get_state("pool_temperature"),
            client(FakeFetch()).get_state("light.living_room"),
        ]
        self.assertGreaterEqual(len(cases), 14)
        for result in cases:
            encoded = json.dumps(result)
            self.assertIsInstance(result, dict)
            self.assertTrue(result)
            self.assertNotIn(encoded, ("", "{}", "null", '""'))
            self.assertNotEqual(encoded.strip(), "")
            self.assertIs(result["ok"], False)
            self.assertIsInstance(result["error"], str)
            self.assertTrue(result["error"].strip())
            self.assertIn(result["error"], ERRORS)
            self.assertNotIn("value", result)
            self.assertNotIn(TOKEN, encoded)
            self.assertNotIn(TOKEN, repr(result))

    def test_not_configured_does_not_call_home_assistant(self):
        fetch = FakeFetch(200, ha_body("21.4"))
        entities = {"pool_temperature": Reading("sensor.pool_temperature", True)}
        bad = [
            HomeAssistant("", TOKEN, entities, fetch=fetch),
            HomeAssistant(URL, "", entities, fetch=fetch),
            HomeAssistant("homeassistant.local:8123", TOKEN, entities, fetch=fetch),
            HomeAssistant(f"http://user:{TOKEN}@homeassistant.local:8123", TOKEN, entities, fetch=fetch),
            HomeAssistant(URL + "/api", TOKEN, entities, fetch=fetch),
            HomeAssistant(URL + "?token=" + TOKEN, TOKEN, entities, fetch=fetch),
        ]
        for ha in bad:
            assert_failure(self, ha.get_state("pool_temperature"), "not_configured")
            self.assertFalse(TOKEN in repr(ha))
            self.assertFalse(TOKEN in ha._url)
        self.assertEqual(fetch.calls, [])

    def test_from_env_reads_the_mapping_it_is_given(self):
        ha = HomeAssistant.from_env({
            "HOME_ASSISTANT_URL": URL,
            "HOME_ASSISTANT_TOKEN": TOKEN,
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            "HA_OFFICE_TEMPERATURE": "text:sensor.office_status",
        })
        self.assertEqual(ha._url, URL)
        self.assertTrue(ha._token == TOKEN)
        self.assertEqual(ha.names, ["office_temperature", "pool_temperature"])
        self.assertEqual(ha._entities["pool_temperature"].entity_id, "sensor.pool_temperature")
        self.assertTrue(ha._entities["pool_temperature"].numeric)
        self.assertFalse(ha._entities["office_temperature"].numeric)
        self.assertEqual(ha._entities["office_temperature"].entity_id, "sensor.office_status")
        empty = HomeAssistant.from_env({})
        self.assertEqual(empty.names, [])
        assert_failure(self, empty.get_state("pool_temperature"), "unknown_name")

    def test_empty_mapping_does_not_fall_through_to_the_process_environment(self):
        with patch.dict(os.environ, {
            "HOME_ASSISTANT_URL": "http://from-process.example:8123",
            "HOME_ASSISTANT_TOKEN": "process-token-not-from-dotenv",
            "HA_POOL_TEMPERATURE": "sensor.from_process",
            "HOME_ASSISTANT_ENTITY_POOL_TEMPERATURE": "sensor.from_process",
        }):
            ha = HomeAssistant.from_env({})
        assert_failure(self, ha.get_state("pool_temperature"), "unknown_name")
        self.assertEqual(ha._url, "")
        self.assertEqual(ha._token, "")
        self.assertEqual(ha.names, [])

    def test_implicit_environment_loads_project_dotenv_before_reading(self):
        """The smoke test passes no mapping, so from_env must load server/.env first."""
        server = Path(__file__).resolve().parents[1]
        if not (server / ".env").is_file():
            self.skipTest("server/.env is not present")
        script = """
import sys
from brain import home_assistant
assert "brain.config" not in sys.modules
from brain.home_assistant import HomeAssistant
ha = HomeAssistant.from_env()
assert "brain.config" in sys.modules
sys.stdout.write("loaded\\n")
sys.stdout.write(" ".join(ha.names) + "\\n")
"""
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("HOME_ASSISTANT_") or key.startswith("HA_"):
                del env[key]
        env["PYTHONPATH"] = str(server)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=server,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("loaded\n"), proc.stdout)
        self.assertNotIn(".", proc.stdout)
        self.assertNotIn("Bearer", proc.stdout)
        self.assertNotIn("Bearer", proc.stderr)

    def test_ha_keys_become_sorted_semantic_names(self):
        entities, warnings = readings_from_env({
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            "HA_LIVING_ROOM_TEMPERATURE": "number:sensor.living_room_temperature",
            "HA_OFFICE_TEMPERATURE": "sensor.office_temperature",
            "HA_PRINTER_PROGRESS": "sensor.printer_progress",
            "HA_PRINTER_NOZZLE_TEMP": "sensor.printer_nozzle_temperature",
            "HA_PRINTER_BED_TEMP": "sensor.printer_bed_temperature",
            "LLM_API_KEY": "not-a-reading",
        })
        self.assertEqual(warnings, [])
        self.assertEqual(list(entities), [
            "living_room_temperature",
            "office_temperature",
            "pool_temperature",
            "printer_bed_temp",
            "printer_nozzle_temp",
            "printer_progress",
        ])
        self.assertTrue(all(item.numeric for item in entities.values()))
        self.assertEqual(entities["living_room_temperature"].entity_id, "sensor.living_room_temperature")
        self.assertNotIn("sensor.living_room_temperature", "".join(warnings))


class CatalogueTests(unittest.TestCase):
    def test_control_keys_are_not_readings(self):
        entities, warnings = readings_from_env({
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            "HA_OFFICE_DESK_LAMP": "text:switch.office_desk_lamp",
            "HA_CONTROL_OFFICE_DESK_LAMP": "sensor.would_be_a_reading",
            "HA_CONTROL_KITCHEN_LAMP": "light:switch.kitchen_lamp",
        })
        self.assertEqual(list(entities), ["office_desk_lamp", "pool_temperature"])
        self.assertEqual(entities["office_desk_lamp"].entity_id, "switch.office_desk_lamp")
        self.assertNotIn("control_office_desk_lamp", entities)
        self.assertNotIn("control_kitchen_lamp", entities)
        self.assertEqual(warnings, [])
        self.assertTrue(all(
            item.entity_id != "sensor.would_be_a_reading" for item in entities.values()
        ))
        self.assertNotIn("kitchen_lamp", json.dumps([item.entity_id for item in entities.values()]))

    def test_text_is_opt_in_and_a_bare_value_stays_numeric(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            ha = HomeAssistant.from_env({
                "HOME_ASSISTANT_URL": URL,
                "HOME_ASSISTANT_TOKEN": TOKEN,
                "HA_OFFICE_DESK_LAMP": "text:switch.office_desk_lamp",
                "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            })
        self.assertEqual(ha._entities["office_desk_lamp"].entity_id, "switch.office_desk_lamp")
        self.assertFalse(ha._entities["office_desk_lamp"].numeric)
        self.assertTrue(ha._entities["pool_temperature"].numeric)
        fetch = FakeFetch(200, ha_body("on", None))
        ha._fetch = fetch
        result = ha.get_state("office_desk_lamp")
        self.assertEqual(result, {"ok": True, "name": "office_desk_lamp", "value": "on", "unit": None})
        self.assertNotIn("switch.office_desk_lamp", json.dumps(result))
        self.assertNotIn("switch.", err.getvalue())
        self.assertFalse(hasattr(ha, "call_service"))

    def test_numeric_prefix_and_rejected_values_name_the_key_only(self):
        secret = "pasted-token-value-should-not-appear"
        entities, warnings = readings_from_env({
            "HA_POOL_TEMPERATURE": "number:sensor.pool_temperature",
            "HA_BAD_PREFIX": "switch:" + secret,
            "HA_TOKEN": secret,
            "ha_pool_temperature": "sensor.pool_temperature",
            "HA_pool_temperature": "sensor.pool_temperature",
            "HA_": "sensor.pool_temperature",
            "HA__POOL": "sensor.pool_temperature",
            "HOME_ASSISTANT_ENTITY_POOL_TEMPERATURE": "sensor.old_pool",
            "HOME_ASSISTANT_URL": URL,
        })
        self.assertEqual(list(entities), ["pool_temperature"])
        self.assertTrue(entities["pool_temperature"].numeric)
        blob = "\n".join(warnings)
        self.assertIn("HA_BAD_PREFIX", blob)
        self.assertIn("HA_TOKEN", blob)
        self.assertIn("HA_pool_temperature", blob)
        self.assertIn("HA__POOL", blob)
        self.assertIn("HOME_ASSISTANT_ENTITY_POOL_TEMPERATURE", blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn("sensor.", blob)
        self.assertNotIn("ha_pool_temperature", blob)
        self.assertNotIn("HOME_ASSISTANT_URL", blob)

    def test_the_thirty_third_reading_is_dropped_by_key_name(self):
        env = {
            f"HA_N{index:02d}": f"sensor.n_{index:02d}"
            for index in range(MAX_READINGS + 1)
        }
        entities, warnings = readings_from_env(env)
        self.assertEqual(len(entities), MAX_READINGS)
        self.assertNotIn("n32", entities)
        self.assertIn("HA_N32", "\n".join(warnings))
        self.assertNotIn("sensor.n_32", "\n".join(warnings))
        self.assertEqual(list(entities), sorted(entities))

    def test_a_text_sensor_accepts_words_and_a_number_sensor_does_not(self):
        ha = HomeAssistant(URL, TOKEN, {
            "printer_status": Reading("sensor.printer_status", False),
            "pool_temperature": Reading("sensor.pool_temperature", True),
        }, fetch=FakeFetch(200, ha_body("idle", None)))
        self.assertEqual(ha.get_state("printer_status")["value"], "idle")
        assert_failure(self, ha.get_state("pool_temperature"), "invalid_state")


class TransportTests(unittest.TestCase):
    def test_default_fetch_is_a_get_and_the_token_stays_in_the_header(self):
        captured = {}

        class Resp:
            status = 200

            def read(self, n=-1):
                return ha_body("21.4")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["auth"] = req.get_header("Authorization")
            captured["data"] = req.data
            captured["timeout"] = timeout
            return Resp()

        with patch("urllib.request.urlopen", fake_urlopen):
            status, body = urllib_get(f"{URL}/api/states/sensor.pool_temperature", TOKEN, 5)
        self.assertEqual(status, 200)
        self.assertIn(b"21.4", body)
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["auth"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, captured["url"])
        self.assertTrue(captured["url"].endswith("/api/states/sensor.pool_temperature"))

    def test_http_error_status_is_returned_without_its_body(self):
        def fake_urlopen(req, timeout=0, context=None):
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(b'{"message":"bad ' + TOKEN.encode() + b'"}'),
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            status, body = urllib_get(URL + "/api/states/sensor.pool_temperature", TOKEN, 5)
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")
        result = HomeAssistant(
            URL,
            TOKEN,
            {"pool_temperature": Reading("sensor.pool_temperature", True)},
            fetch=urllib_get,
        )
        with patch("urllib.request.urlopen", fake_urlopen):
            parsed = result.get_state("pool_temperature")
        assert_failure(self, parsed, "authentication_failed")


class ProductionBoundaryTests(unittest.TestCase):
    def test_module_only_gets_state(self):
        source = Path(home_assistant.__file__).read_text()
        for banned in (
            "call_service",
            "/api/services",
            "turn_on",
            "turn_off",
            "control_light",
            "set_state",
        ):
            self.assertNotIn(banned, source)
        self.assertFalse(hasattr(HomeAssistant, "call_service"))
        self.assertFalse(hasattr(HomeAssistant, "set_state"))
        self.assertFalse(hasattr(HomeAssistant, "control_light"))

    def test_get_home_state_is_a_read_only_tool_and_not_personality(self):
        names = [tool["function"]["name"] for tool in thinking.build_tools(["pool_temperature"])]
        self.assertEqual(names, ["look", "track_face", "get_home_state"])
        self.assertEqual(
            [tool["function"]["name"] for tool in thinking.build_tools([])],
            ["look", "track_face"],
        )
        character = (_BRAIN / "personality.py").read_text()
        self.assertNotIn("HOME_ASSISTANT", character)
        self.assertNotIn("pool_temperature", character)
        example = Path(__file__).resolve().parents[1] / "personal_context.example.txt"
        self.assertNotIn("HOME_ASSISTANT", example.read_text())
        main_text = (_BRAIN / "main.py").read_text()
        self.assertIn("get_home_state", main_text)
        self.assertNotIn("HOME_ASSISTANT_TOKEN", main_text)
        self.assertNotIn("/api/states", main_text)
        self.assertNotIn("/api/services", main_text)


class SmokeCommandTests(unittest.TestCase):
    def test_smoke_prints_the_result_and_not_the_token(self):
        fetch = FakeFetch(200, ha_body("21.4", "°C"))
        ha = client(fetch)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(HomeAssistant, "from_env", return_value=ha), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            code = home_assistant.main(["pool_temperature"])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed, [{
            "ok": True,
            "name": "pool_temperature",
            "value": 21.4,
            "unit": "°C",
        }])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertEqual(fetch.calls[0][0], f"{URL}/api/states/sensor.pool_temperature")

    def test_standalone_smoke_output_never_exposes_the_token(self):
        secret = "do-not-print-this-ha-token"
        fetch = FakeFetch(401, b"")
        ha = HomeAssistant(URL, secret, {
            "pool_temperature": Reading("sensor.pool_temperature", True),
        }, fetch=fetch)
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {
                "HOME_ASSISTANT_URL": URL,
                "HOME_ASSISTANT_TOKEN": secret,
            }),
            patch.object(HomeAssistant, "from_env", return_value=ha),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            code = home_assistant.main(["pool_temperature"])
        blob = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, repr(ha))
        self.assertIn("authentication_failed", out.getvalue())

    def test_smoke_default_names_and_not_configured_hint(self):
        ha = HomeAssistant.from_env({})
        out, err = io.StringIO(), io.StringIO()
        with patch.object(HomeAssistant, "from_env", return_value=ha), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            code = home_assistant.main([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), [])
        self.assertIn("HA_<NAME>", err.getvalue())
        self.assertIn("HOME_ASSISTANT_URL", err.getvalue())
        self.assertIn("HOME_ASSISTANT_TOKEN", err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_smoke_with_no_args_reads_the_configured_names(self):
        fetch = FakeFetch(200, ha_body("17", "°C"))
        ha = HomeAssistant(URL, TOKEN, {
            "pool_temperature": Reading("sensor.pool_temperature", True),
            "office_temperature": Reading("sensor.office_temperature", True),
        }, fetch=fetch)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(HomeAssistant, "from_env", return_value=ha), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            code = home_assistant.main([])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual([item["name"] for item in printed], ["pool_temperature", "office_temperature"])
        self.assertNotIn("sensor.", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_smoke_names_a_missing_entity_mapping(self):
        ha = HomeAssistant(URL, TOKEN, {
            "printer_progress": Reading("", True),
        })
        out, err = io.StringIO(), io.StringIO()
        with patch.object(HomeAssistant, "from_env", return_value=ha), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            code = home_assistant.main(["printer_progress"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())[0]["error"], "entity_not_configured")
        self.assertIn("HA_PRINTER_PROGRESS", err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
