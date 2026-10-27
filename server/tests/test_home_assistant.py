"""The Home Assistant adapter returns a structured result for every lookup.

Ported from V1. HTTP is mocked. These tests do not contact Home Assistant
and do not read a real token.
"""

import io
import json
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from brain import home, home_assistant
from brain.home_assistant import (
    ERRORS,
    MAX_READINGS,
    HomeAssistant,
    Reading,
    check_connection,
    list_entities,
    readings_from_text,
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


class CatalogueTests(unittest.TestCase):
    """V1's HA_<NAME>=<value> lines become "name = value" lines in the dashboard."""

    def test_lines_become_sorted_semantic_names(self):
        entities = readings_from_text(
            "pool_temperature = sensor.pool_temperature\n"
            "living_room_temperature = number:sensor.living_room_temperature\n"
            "\n"
            "office_temperature = sensor.office_temperature\n"
            "printer_progress = sensor.printer_progress\n"
            "printer_nozzle_temp = sensor.printer_nozzle_temperature\n"
            "printer_bed_temp = sensor.printer_bed_temperature\n"
        )
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

    def test_text_is_opt_in_and_a_bare_value_stays_numeric(self):
        entities = readings_from_text(
            "office_desk_lamp = text:switch.office_desk_lamp\n"
            "pool_temperature = sensor.pool_temperature\n"
        )
        ha = HomeAssistant(URL, TOKEN, entities, fetch=FakeFetch(200, ha_body("on", None)))
        self.assertEqual(ha._entities["office_desk_lamp"].entity_id, "switch.office_desk_lamp")
        self.assertFalse(ha._entities["office_desk_lamp"].numeric)
        self.assertTrue(ha._entities["pool_temperature"].numeric)
        result = ha.get_state("office_desk_lamp")
        self.assertEqual(result, {"ok": True, "name": "office_desk_lamp", "value": "on", "unit": None})
        self.assertNotIn("switch.office_desk_lamp", json.dumps(result))
        self.assertFalse(hasattr(ha, "call_service"))

    def test_a_light_line_is_not_a_reading(self):
        with self.assertRaises(ValueError) as caught:
            readings_from_text("office_desk_lamp = light:switch.office_desk_lamp")
        self.assertIn("line 1", str(caught.exception))

    def test_rejected_lines_name_the_line_and_never_the_value(self):
        secret = "pasted-token-value-should-not-appear"
        bad = (
            "pool_temperature = switch:" + secret,
            "pool_temperature = " + secret + " with spaces",
            "Pool_Temperature = sensor.pool_temperature",
            "pool__temperature = sensor.pool_temperature",
            "_pool = sensor.pool_temperature",
            "1pool = sensor.pool_temperature",
            "sensor.pool_temperature = sensor.pool_temperature",
            "pool_temperature sensor.pool_temperature",
            "pool_temperature = ",
            " = sensor.pool_temperature",
            "x" * 65 + " = sensor.pool",
            "pool_temperature = number:" + secret,
            "pool_temperature = text:",
        )
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    readings_from_text("office_temperature = sensor.office\n" + line)
                message = str(caught.exception)
                self.assertIn("line 2", message)
                self.assertNotIn(secret, message)

    def test_a_duplicate_name_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            readings_from_text("pool_temperature = sensor.a\npool_temperature = sensor.b")
        self.assertIn("line 2", str(caught.exception))
        self.assertIn("twice", str(caught.exception))

    def test_the_thirty_third_reading_is_rejected_by_line(self):
        lines = [f"n{index:02d} = sensor.n_{index:02d}" for index in range(MAX_READINGS + 1)]
        self.assertEqual(len(readings_from_text("\n".join(lines[:MAX_READINGS]))), MAX_READINGS)
        with self.assertRaises(ValueError) as caught:
            readings_from_text("\n".join(lines))
        self.assertIn(f"line {MAX_READINGS + 1}", str(caught.exception))
        self.assertIn(str(MAX_READINGS), str(caught.exception))
        self.assertNotIn("sensor.n_32", str(caught.exception))

    def test_blank_text_is_no_readings(self):
        self.assertEqual(readings_from_text(""), {})
        self.assertEqual(readings_from_text("\n  \n"), {})

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

    def test_importing_does_not_need_certifi(self):
        """The launcher imports this through settings.py without the brain's dependencies."""
        source = Path(home_assistant.__file__).read_text()
        self.assertNotIn("\nimport certifi", source)
        self.assertIn("import certifi", source)   # loaded when a request is made
        self.assertIsNotNone(sys.modules.get("brain.home_assistant"))


class DashboardHelperTests(unittest.TestCase):
    """check_connection and list_entities: read-only, the token only as a header, nothing echoed."""

    def test_check_connection_maps_each_outcome(self):
        cases = (
            (FakeFetch(200, b'{"message": "API running."}'), {"ok": True}),
            (FakeFetch(401, b""), {"ok": False, "error": "authentication_failed"}),
            (FakeFetch(403, b""), {"ok": False, "error": "authentication_failed"}),
            (FakeFetch(404, b""), {"ok": False, "error": "request_failed"}),
            (FakeFetch(500, b""), {"ok": False, "error": "request_failed"}),
            (FakeFetch(error=TimeoutError()), {"ok": False, "error": "timeout"}),
            (FakeFetch(error=urllib.error.URLError(TimeoutError())), {"ok": False, "error": "timeout"}),
            (FakeFetch(error=urllib.error.URLError(OSError(f"refused {TOKEN}"))),
             {"ok": False, "error": "connection_failed"}),
            (FakeFetch(error=OSError("down")), {"ok": False, "error": "connection_failed"}),
        )
        for fetch, expected in cases:
            with self.subTest(expected=expected):
                result = check_connection(URL, TOKEN, fetch=fetch)
                self.assertEqual(result, expected)
                self.assertEqual(fetch.calls[0][0], URL + "/api/")
                self.assertEqual(fetch.calls[0][1], TOKEN)
                self.assertNotIn(TOKEN, json.dumps(result))

    def test_check_connection_without_a_hub_makes_no_request(self):
        fetch = FakeFetch(200, b"{}")
        for url, token in (("", TOKEN), (URL, ""), (URL + "/api", TOKEN), (f"http://u:{TOKEN}@h:8123", TOKEN),
                           ("ftp://h", TOKEN), (URL + "?t=" + TOKEN, TOKEN)):
            with self.subTest(url=url):
                self.assertEqual(check_connection(url, token, fetch=fetch), {"ok": False, "error": "not_configured"})
        self.assertEqual(fetch.calls, [])

    def test_list_entities_keeps_four_short_fields_and_sorts(self):
        states = [
            {"entity_id": "switch.office_desk_lamp", "state": "off",
             "attributes": {"friendly_name": "Office Desk Lamp", "secret_attr": "hidden"}, "context": {"id": "x"}},
            {"entity_id": "sensor.pool_temperature", "state": "27.5",
             "attributes": {"friendly_name": "Pool\nTemperature", "unit_of_measurement": "°C"}},
            {"entity_id": "not an entity", "state": "on"},
            {"entity_id": "light.porch", "state": "x" * 500, "attributes": "not a dict"},
            "junk",
        ]
        fetch = FakeFetch(200, json.dumps(states).encode())
        result = list_entities(URL, TOKEN, fetch=fetch)
        self.assertEqual(fetch.calls[0][0], URL + "/api/states")
        self.assertTrue(result["ok"])
        self.assertEqual([e["entity_id"] for e in result["entities"]],
                         ["light.porch", "sensor.pool_temperature", "switch.office_desk_lamp"])
        pool = result["entities"][1]
        self.assertEqual(pool, {"entity_id": "sensor.pool_temperature", "name": "Pool Temperature",
                                "state": "27.5", "unit": "°C"})
        self.assertEqual(len(result["entities"][0]["state"]), 40)
        encoded = json.dumps(result)
        self.assertNotIn("secret_attr", encoded)
        self.assertNotIn("hidden", encoded)
        self.assertNotIn("context", encoded)
        self.assertNotIn(TOKEN, encoded)

    def test_list_entities_failures(self):
        cases = (
            (FakeFetch(401, b""), "authentication_failed"),
            (FakeFetch(500, b""), "request_failed"),
            (FakeFetch(200, b"{not json"), "invalid_response"),
            (FakeFetch(200, b'{"a": 1}'), "invalid_response"),
            (FakeFetch(error=urllib.error.URLError("down")), "connection_failed"),
        )
        for fetch, error in cases:
            with self.subTest(error=error):
                self.assertEqual(list_entities(URL, TOKEN, fetch=fetch), {"ok": False, "error": error})
        self.assertEqual(list_entities("", TOKEN, fetch=FakeFetch()), {"ok": False, "error": "not_configured"})

    def test_list_entities_default_transport_allows_a_larger_body(self):
        seen = {}

        def fake_get(url, token, timeout, max_body=home_assistant._MAX_BODY):
            seen["max_body"] = max_body
            return 200, b"[]"

        with patch.object(home_assistant, "urllib_get", fake_get):
            self.assertEqual(list_entities(URL, TOKEN), {"ok": True, "entities": []})
        self.assertGreater(seen["max_body"], home_assistant._MAX_BODY)

    def test_check_url(self):
        for good in ("", URL, URL + "/", "https://ha.example.com", "http://192.168.1.5:8123"):
            with self.subTest(url=good):
                self.assertEqual(home_assistant.check_url(good), good.strip())
        for bad in ("homeassistant.local:8123", "ftp://h", URL + "/api", "http://user:pw@h:8123",
                    URL + "?token=abc", URL + "#x", "http://", "http://h :8123", "javascript:alert(1)"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as caught:
                    home_assistant.check_url(bad)
                self.assertNotIn("pw", str(caught.exception))
                self.assertNotIn("abc", str(caught.exception))


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
            "urlopen(req, data",
            'method="POST"',
        ):
            self.assertNotIn(banned, source)
        self.assertFalse(hasattr(HomeAssistant, "call_service"))
        self.assertFalse(hasattr(HomeAssistant, "set_state"))
        self.assertFalse(hasattr(HomeAssistant, "control_light"))

    def test_get_home_state_is_a_read_only_tool_and_not_personality(self):
        names = [tool["function"]["name"] for tool in home.build_tools(["pool_temperature"])]
        self.assertEqual(names, ["get_home_state"])
        self.assertEqual(home.build_tools([]), [])
        character = (_BRAIN / "personality.py").read_text()
        self.assertNotIn("HOME_ASSISTANT", character)
        self.assertNotIn("pool_temperature", character)
        example = Path(__file__).resolve().parents[1] / "personal_context.example.txt"
        self.assertNotIn("HOME_ASSISTANT", example.read_text())
        main_text = (_BRAIN / "main.py").read_text()
        self.assertIn("home.ACTIONS", main_text)
        self.assertNotIn("HOME_ASSISTANT_TOKEN", main_text)
        self.assertNotIn("/api/states", main_text)
        self.assertNotIn("/api/services", main_text)


if __name__ == "__main__":
    unittest.main()
