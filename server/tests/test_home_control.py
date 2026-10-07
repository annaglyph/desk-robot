"""control_light turns one authorised light on or off.

HTTP is mocked. These tests do not contact Home Assistant and do not read
HOME_ASSISTANT_TOKEN from the environment.

The live-model prompts at the bottom are not run here. They are the
acceptance set for a later model run, after a real lamp check.
"""

import io
import json
import os
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain import home_assistant, thinking
from brain.home_assistant import HomeAssistant, Reading, readings_from_env
from brain.home_control import (
    ERRORS,
    MAX_CONTROLS,
    VERIFY_PAUSES,
    HomeControl,
    Light,
    controls_from_env,
    set_light_for_tool,
    urllib_post,
    _seal,
)

TOKEN = "test-token"
URL = "http://homeassistant.local:8123"
LAMP = "switch.office_desk_lamp"
PORCH = "light.porch_lamp"

# Later live-model acceptance. This suite does not call a model.
# Identity must be the full listed name. Current state must be read fresh.
# A compound request must not be partly carried out. The server still
# validates any control_light call that arrives; it does not roll back.
LIVE_MODEL_ACCEPTANCE = (
    "Turn the office desk lamp on.",
    "Can you turn the office desk lamp on?",
    "Turn office desk lamp off.",
    "Turn the desk lamp on.",
    "Turn the lamp on.",
    "Turn every light off.",
    "Don't turn the office desk lamp on.",
    "It's dark in here.",
    "The desk lamp is annoying.",
    "Turn the office desk lamp on and the printer off.",
    "Is the office desk lamp on?",
    "Rocky is the office desk lamp on.",
    "What is the pool temperature?",
    "What is the office temperature?",
    "What is the printer progress?",
    "What did you just do?",
    "Is the desk lamp on?",
    "Never mind. What is the pool temperature?",
)


def state_body(state: str) -> bytes:
    return json.dumps({
        "entity_id": LAMP,
        "state": state,
        "attributes": {"brightness": 255, "friendly_name": "Office Desk Lamp"},
    }).encode()


class Transport:
    def __init__(self):
        self.posts = []
        self.gets = []
        self._posts = []
        self._gets = []

    def push_post(self, status=200, body=b"", error=None):
        self._posts.append((status, body, error))

    def push_get(self, status=200, body=b"", error=None):
        self._gets.append((status, body, error))

    def post(self, url, token, body, timeout):
        self.posts.append((url, token, body, timeout))
        status, data, error = self._posts.pop(0)
        if error is not None:
            raise error
        return status, data

    def get(self, url, token, timeout):
        self.gets.append((url, token, timeout))
        status, data, error = self._gets.pop(0)
        if error is not None:
            raise error
        return status, data


def client(transport, lights=None, pauses=None, **kwargs) -> HomeControl:
    catalog = {"office_desk_lamp": Light(LAMP)} if lights is None else lights
    pause = (lambda _seconds: None) if pauses is None else pauses.append
    return HomeControl(
        kwargs.pop("url", URL),
        kwargs.pop("token", TOKEN),
        catalog,
        post=transport.post,
        fetch=transport.get,
        pause=pause,
        **kwargs,
    )


def _chunk(content=None, tool_calls=None):
    calls = None
    if tool_calls is not None:
        calls = [
            SimpleNamespace(
                index=item["index"],
                id=item.get("id"),
                function=SimpleNamespace(name=item.get("name"), arguments=item.get("arguments")),
            )
            for item in tool_calls
        ]
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls))])


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        yield from self._chunks

    def close(self) -> None:
        return None


class _Completions:
    def __init__(self, rounds):
        self.rounds = rounds
        self.i = 0

    def create(self, **kwargs):
        chunks = self.rounds[self.i]
        self.i += 1
        return _Stream(chunks)


def light_tool(names=("office_desk_lamp",)):
    tools = thinking.build_tools([], list(names))
    return tools[-1]["function"]


class CatalogueTests(unittest.TestCase):
    def test_light_prefix_is_required_and_only_light_or_switch_entities_parse(self):
        lights, warnings = controls_from_env({
            "HA_CONTROL_OFFICE_DESK_LAMP": "light:" + LAMP,
            "HA_CONTROL_PORCH_LAMP": "light:" + PORCH,
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            "HA_OFFICE_DESK_LAMP": "text:" + LAMP,
        })
        self.assertEqual(warnings, [])
        self.assertEqual(list(lights), ["office_desk_lamp", "porch_lamp"])
        self.assertEqual(lights["office_desk_lamp"].entity_id, LAMP)
        self.assertEqual(lights["porch_lamp"].entity_id, PORCH)

    def test_a_read_line_and_a_bare_entity_do_not_grant_control(self):
        secret = "pasted-token-value-should-not-appear"
        lights, warnings = controls_from_env({
            "HA_OFFICE_DESK_LAMP": "text:" + LAMP,
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
            "HA_CONTROL_OFFICE_DESK_LAMP": LAMP,
            "HA_CONTROL_PORCH_LAMP": "text:" + PORCH,
            "HA_CONTROL_DOOR": "switch:" + LAMP,
            "HA_CONTROL_BAD": secret,
            "HA_CONTROL_": "light:" + LAMP,
            "HA_CONTROL__LAMP": "light:" + LAMP,
            "HOME_ASSISTANT_URL": URL,
        })
        self.assertEqual(lights, {})
        blob = "\n".join(warnings)
        self.assertIn("HA_CONTROL_OFFICE_DESK_LAMP", blob)
        self.assertIn("HA_CONTROL_PORCH_LAMP", blob)
        self.assertIn("HA_CONTROL_DOOR", blob)
        self.assertIn("HA_CONTROL_BAD", blob)
        self.assertIn("HA_CONTROL_", blob)
        self.assertIn("HA_CONTROL__LAMP", blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn(LAMP, blob)
        self.assertNotIn("switch.", blob)
        self.assertNotIn("HA_POOL_TEMPERATURE", blob)
        self.assertNotIn("HA_OFFICE_DESK_LAMP", blob)
        self.assertNotIn("HOME_ASSISTANT_URL", blob)

    def test_other_domains_are_rejected(self):
        rejected = (
            "lock.front_door",
            "climate.office",
            "cover.garage",
            "scene.movie",
            "script.bedtime",
            "automation.morning",
            "fan.ceiling",
            "media_player.speaker",
            "input_boolean.guest",
        )
        env = {
            f"HA_CONTROL_BAD_{index}": "light:" + entity
            for index, entity in enumerate(rejected)
        }
        env["HA_CONTROL_OFFICE_DESK_LAMP"] = "light:" + LAMP
        lights, warnings = controls_from_env(env)
        self.assertEqual(list(lights), ["office_desk_lamp"])
        blob = "\n".join(warnings)
        for entity in rejected:
            self.assertNotIn(entity, blob)
            self.assertNotIn(entity.split(".", 1)[0] + ".", "".join(lights))
        self.assertGreaterEqual(len(warnings), len(rejected))

    def test_read_and_control_catalogues_stay_independent(self):
        env = {
            "HA_OFFICE_DESK_LAMP": "text:switch.read_side",
            "HA_CONTROL_OFFICE_DESK_LAMP": "light:switch.control_side",
            "HA_POOL_TEMPERATURE": "sensor.pool_temperature",
        }
        readings, read_warnings = readings_from_env(env)
        lights, warnings = controls_from_env(env)
        self.assertEqual(readings["office_desk_lamp"].entity_id, "switch.read_side")
        self.assertEqual(lights["office_desk_lamp"].entity_id, "switch.control_side")
        self.assertIn("pool_temperature", readings)
        self.assertNotIn("pool_temperature", lights)
        self.assertEqual(read_warnings, [])
        self.assertEqual(warnings, [
            "HA_CONTROL_OFFICE_DESK_LAMP and HA_OFFICE_DESK_LAMP do not refer to the same entity.",
        ])
        self.assertNotIn("switch.", warnings[0])

    def test_the_same_entity_on_both_lines_is_quiet(self):
        _lights, warnings = controls_from_env({
            "HA_OFFICE_DESK_LAMP": "text:" + LAMP,
            "HA_CONTROL_OFFICE_DESK_LAMP": "light:" + LAMP,
        })
        self.assertEqual(warnings, [])

    def test_control_without_a_reading_is_allowed(self):
        lights, warnings = controls_from_env({
            "HA_CONTROL_OFFICE_DESK_LAMP": "light:" + LAMP,
        })
        self.assertEqual(list(lights), ["office_desk_lamp"])
        self.assertEqual(warnings, [])

    def test_the_ninth_light_is_dropped_by_key_name(self):
        env = {
            f"HA_CONTROL_LAMP_{index:02d}": f"light:switch.lamp_{index:02d}"
            for index in range(MAX_CONTROLS + 1)
        }
        lights, warnings = controls_from_env(env)
        self.assertEqual(len(lights), MAX_CONTROLS)
        self.assertNotIn("lamp_08", lights)
        blob = "\n".join(warnings)
        self.assertIn("HA_CONTROL_LAMP_08", blob)
        self.assertNotIn("switch.lamp_08", blob)

    def test_a_blank_control_line_is_unset(self):
        lights, warnings = controls_from_env({
            "HA_CONTROL_OFFICE_DESK_LAMP": "   ",
            "HOME_ASSISTANT_TOKEN": TOKEN,
        })
        self.assertEqual(lights, {})
        self.assertEqual(warnings, [])

    def test_empty_mapping_does_not_read_the_process_environment(self):
        with patch.dict(os.environ, {
            "HOME_ASSISTANT_URL": "http://from-process.example:8123",
            "HOME_ASSISTANT_TOKEN": "process-token-not-from-dotenv",
            "HA_CONTROL_OFFICE_DESK_LAMP": "light:" + LAMP,
        }):
            control = HomeControl.from_env({})
        self.assertEqual(control.names, [])
        self.assertEqual(control._url, "")
        self.assertEqual(control._token, "")
        self.assertNotIn(TOKEN, repr(control))
        self.assertNotIn(LAMP, repr(control))


class SchemaTests(unittest.TestCase):
    def test_a_readable_name_does_not_add_the_control_tool(self):
        tools = thinking.build_tools(["office_desk_lamp", "pool_temperature"])
        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["look", "track_face", "get_home_state"],
        )

    def test_no_lights_omits_the_tool_and_lights_without_readings_omit_the_read_tool(self):
        self.assertEqual(
            [tool["function"]["name"] for tool in thinking.build_tools([], [])],
            ["look", "track_face"],
        )
        self.assertEqual(
            [tool["function"]["name"] for tool in thinking.build_tools([])],
            ["look", "track_face"],
        )
        only = [tool["function"]["name"] for tool in thinking.build_tools([], ["office_desk_lamp"])]
        self.assertEqual(only, ["look", "track_face", "control_light"])

    def test_a_dotted_name_is_not_advertised(self):
        tools = thinking.build_tools([], [LAMP])
        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["look", "track_face"],
        )

    def test_schema_enums_are_only_authorised_names_and_on_off(self):
        tool = light_tool(["porch_lamp", "office_desk_lamp"])
        params = tool["parameters"]
        self.assertEqual(tool["name"], "control_light")
        self.assertEqual(params["required"], ["name", "action"])
        self.assertIs(params["additionalProperties"], False)
        self.assertEqual(set(params["properties"]), {"name", "action"})
        self.assertEqual(
            params["properties"]["name"]["enum"],
            ["office_desk_lamp", "porch_lamp"],
        )
        self.assertEqual(params["properties"]["action"]["enum"], ["on", "off"])
        encoded = json.dumps(tool)
        for banned in ("turn_on", "turn_off", "/api/", "entity_id", "switch.", "light.", LAMP, PORCH):
            self.assertNotIn(banned, encoded)

    def test_description_states_when_to_call_and_when_not_to(self):
        text = light_tool()["description"]
        self.assertIn(thinking._device_identity(["office_desk_lamp"]), text)
        self.assertIn('"office desk lamp" identifies office_desk_lamp.', text)
        self.assertIn('"desk lamp" does not identify office_desk_lamp.', text)
        self.assertIn('"office lamp" does not identify office_desk_lamp.', text)
        self.assertIn('"lamp" does not identify office_desk_lamp.', text)
        self.assertIn(
            "All meaningful words from the listed name, in order, are required to identify it.",
            text,
        )
        self.assertIn("One listed name is not a default.", text)
        self.assertIn("Do not invent an alias.", text)
        self.assertIn("request still waiting in the conversation", text)
        self.assertIn("Can you turn the office desk lamp on?", text)
        self.assertIn("identified one authorised light exactly", text)
        self.assertIn("Do not ask whether to proceed.", text)
        self.assertIn("Do not call get_home_state first.", text)
        self.assertIn("get_home_state", text)
        self.assertIn("because a light was mentioned", text)
        self.assertIn("seems dark", text)
        self.assertIn("annoying", text)
        self.assertIn("told not to change it", text)
        self.assertIn("every light", text)
        self.assertIn("all lights", text)
        self.assertIn('One authorised light is not "every light".', text)
        self.assertIn("complete requested operation cannot be performed", text)
        self.assertIn("partial substitute", text)
        self.assertIn("Turning the office desk lamp on and the printer off", text)
        self.assertIn("do not turn the office desk lamp on", text)
        self.assertIn("describes this command only", text)
        self.assertIn("immediately after this command", text)
        self.assertIn("still in that state later", text)
        self.assertIn("state_unconfirmed", text)
        self.assertIn("Never invent success.", text)
        self.assertIn("Authorised lights: office_desk_lamp.", text)
        self.assertIn(
            "The authorised light the human identified exactly.",
            light_tool()["parameters"]["properties"]["name"]["description"],
        )
        self.assertNotIn("Can you turn the desk lamp on?", text)
        self.assertNotIn("switch the desk lamp off", text)
        self.assertNotIn("are explicit requests", text)
        self.assertNotIn("is the only truth", text)
        self.assertNotIn("toggle", text)
        self.assertNotIn("brightness", text)

    def test_partial_examples_follow_the_listed_name(self):
        porch = light_tool(["porch_lamp"])["description"]
        self.assertIn('"porch lamp" identifies porch_lamp.', porch)
        self.assertIn('"lamp" does not identify porch_lamp.', porch)
        self.assertNotIn("desk lamp", porch)

        single = light_tool(["lamp"])["description"]
        self.assertNotIn('"lamp" does not identify lamp.', single)
        self.assertIn("A shorter or different phrase does not identify it.", single)

        many = light_tool(["office_desk_lamp", "living_room_desk_lamp"])["description"]
        self.assertIn("One listed name is not a default.", many)
        self.assertIn(
            "A shared ending such as desk lamp does not choose among listed names that end that way.",
            many,
        )

    def test_identity_guidance_is_shared_and_is_not_a_matcher(self):
        names = ["office_desk_lamp"]
        guidance = thinking._device_identity(names)
        self.assertEqual(
            thinking._device_identity.__code__.co_varnames[:thinking._device_identity.__code__.co_argcount],
            ("names",),
        )
        tools = thinking.build_tools(names, names)
        by_name = {tool["function"]["name"]: tool["function"] for tool in tools}
        self.assertIn(guidance, by_name["get_home_state"]["description"])
        self.assertIn(guidance, by_name["control_light"]["description"])
        self.assertIn(
            "The listed name the human identified exactly.",
            by_name["get_home_state"]["parameters"]["properties"]["name"]["description"],
        )

    def test_live_model_acceptance_cases_are_covered_by_the_description(self):
        """Guidance only. Run LIVE_MODEL_ACCEPTANCE against a model later."""
        text = light_tool()["description"]
        self.assertIn("Turn the office desk lamp on and the printer off.", LIVE_MODEL_ACCEPTANCE)
        self.assertIn("Don't turn the office desk lamp on.", LIVE_MODEL_ACCEPTANCE)
        self.assertIn("It's dark in here.", LIVE_MODEL_ACCEPTANCE)
        self.assertIn("Is the office desk lamp on?", LIVE_MODEL_ACCEPTANCE)
        self.assertIn("Is the desk lamp on?", LIVE_MODEL_ACCEPTANCE)
        self.assertIn("complete requested operation cannot be performed", text)
        self.assertIn("do not call this tool", text)
        self.assertIn("still in that state later", text)
        for prompt in LIVE_MODEL_ACCEPTANCE:
            self.assertIsInstance(prompt, str)
            self.assertGreater(len(prompt), 10)


class PerformTests(unittest.TestCase):
    def test_on_posts_the_configured_switch_then_reads_state(self):
        transport = Transport()
        transport.push_post(200, b'[{"entity_id":"switch.unrelated","state":"on"}]')
        transport.push_get(200, state_body("on"))
        pauses = []
        result = client(transport, pauses=pauses).set_light("office_desk_lamp", "on")
        self.assertEqual(result, {
            "ok": True,
            "name": "office_desk_lamp",
            "action": "on",
            "outcome": "confirmed",
            "state": "on",
        })
        self.assertEqual(pauses, [])
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(len(transport.gets), 1)
        url, token, body, timeout = transport.posts[0]
        self.assertEqual(url, f"{URL}/api/services/switch/turn_on")
        self.assertNotIn("return_response", url)
        self.assertEqual(token, TOKEN)
        self.assertNotIn(TOKEN, url)
        self.assertEqual(json.loads(body), {"entity_id": LAMP})
        self.assertEqual(timeout, home_assistant.TIMEOUT)
        get_url, get_token, get_timeout = transport.gets[0]
        self.assertEqual(get_url, f"{URL}/api/states/{LAMP}")
        self.assertEqual(get_token, TOKEN)
        self.assertEqual(get_timeout, 2.0)
        encoded = json.dumps(result)
        self.assertNotIn(LAMP, encoded)
        self.assertNotIn("turn_on", encoded)
        self.assertNotIn("brightness", encoded)
        self.assertNotIn("friendly_name", encoded)
        self.assertNotIn("unrelated", encoded)
        self.assertNotIn(TOKEN, encoded)

    def test_off_for_a_light_entity_uses_that_domain(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        transport.push_get(200, state_body("off"))
        result = client(transport, {"porch_lamp": Light(PORCH)}).set_light("porch_lamp", "off")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["state"], "off")
        self.assertEqual(transport.posts[0][0], f"{URL}/api/services/light/turn_off")
        self.assertEqual(json.loads(transport.posts[0][2]), {"entity_id": PORCH})
        self.assertNotIn("turn_off", json.dumps(result))
        self.assertNotIn(PORCH, json.dumps(result))

    def test_model_fields_cannot_choose_the_entity_or_the_service(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        transport.push_get(200, state_body("off"))
        text, image = set_light_for_tool(client(transport), {
            "name": "office_desk_lamp",
            "action": "off",
            "entity_id": "switch.evil",
            "service": "turn_on",
            "domain": "lock",
            "brightness": 10,
        })
        self.assertIsNone(image)
        self.assertEqual(json.loads(transport.posts[0][2]), {"entity_id": LAMP})
        self.assertIn("/api/services/switch/turn_off", transport.posts[0][0])
        self.assertNotIn("evil", transport.posts[0][0])
        self.assertNotIn("lock", transport.posts[0][0])
        self.assertNotIn("evil", text)
        self.assertNotIn(LAMP, text)
        self.assertNotIn(TOKEN, text)

    def test_http_200_means_the_service_was_accepted_not_that_the_state_was_confirmed(self):
        transport = Transport()
        transport.push_post(200, b'[{"entity_id":"switch.unrelated","state":"on"}]')
        for _ in range(3):
            transport.push_get(200, state_body("off"))
        pauses = []
        result = client(transport, pauses=pauses).set_light("office_desk_lamp", "on")
        self.assertEqual(result, {
            "ok": False,
            "name": "office_desk_lamp",
            "action": "on",
            "error": "state_unconfirmed",
            "state": "off",
        })
        self.assertNotIn("outcome", result)
        self.assertIs(result["ok"], False)
        self.assertEqual(pauses, [0.4, 0.8])
        self.assertEqual(VERIFY_PAUSES, (0.4, 0.8))
        self.assertEqual(len(transport.gets), 3)
        self.assertNotIn("unrelated", json.dumps(result))
        self.assertNotIn(LAMP, json.dumps(result))

    def test_a_later_read_can_confirm_after_the_first_still_shows_the_old_state(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        transport.push_get(200, state_body("off"))
        transport.push_get(200, state_body("on"))
        pauses = []
        result = client(transport, pauses=pauses).set_light("office_desk_lamp", "on")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["state"], "on")
        self.assertEqual(pauses, [0.4])
        self.assertEqual(len(transport.gets), 2)

    def test_unavailable_after_the_reads_is_not_success(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        for _ in range(3):
            transport.push_get(200, state_body("unavailable"))
        result = client(transport, pauses=[]).set_light("office_desk_lamp", "on")
        self.assertEqual(result["error"], "entity_unavailable")
        self.assertNotIn("state", result)
        self.assertIs(result["ok"], False)

    def test_a_missing_entity_stops_without_another_read(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        transport.push_get(404, b"")
        pauses = []
        result = client(transport, pauses=pauses).set_light("office_desk_lamp", "off")
        self.assertEqual(result["error"], "entity_not_found")
        self.assertEqual(pauses, [])
        self.assertEqual(len(transport.gets), 1)
        self.assertNotIn("state", result)

    def test_a_failed_verification_read_is_unconfirmed_without_a_state(self):
        transport = Transport()
        transport.push_post(200, b'[{"state":"on"}]')
        transport.push_get(error=urllib.error.URLError(OSError("down")))
        transport.push_get(error=TimeoutError("slow"))
        transport.push_get(200, state_body("unknown"))
        result = client(transport, pauses=[]).set_light("office_desk_lamp", "on")
        self.assertEqual(result["error"], "state_unconfirmed")
        self.assertNotIn("state", result)
        self.assertIs(result["ok"], False)
        self.assertEqual(len(transport.gets), 3)

    def test_the_opposite_state_is_reported_and_not_treated_as_success(self):
        transport = Transport()
        transport.push_post(200, b"[]")
        for _ in range(3):
            transport.push_get(200, state_body("on"))
        result = client(transport, pauses=[]).set_light("office_desk_lamp", "off")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"], "state_unconfirmed")
        self.assertEqual(result["state"], "on")
        self.assertEqual(result["action"], "off")

    def test_post_failures_do_not_verify_and_do_not_claim_a_state(self):
        cases = (
            (401, None, "authentication_failed"),
            (403, None, "authentication_failed"),
            (400, None, "request_failed"),
            (404, None, "request_failed"),
            (500, None, "request_failed"),
            (201, None, "request_failed"),
            (None, TimeoutError("slow"), "timeout"),
            (None, urllib.error.URLError(TimeoutError("slow")), "timeout"),
            (None, urllib.error.URLError(OSError(f"refused {TOKEN}")), "connection_failed"),
            (None, OSError("down"), "connection_failed"),
        )
        for status, error, expected in cases:
            with self.subTest(expected=expected, status=status):
                transport = Transport()
                transport.push_post(status or 0, b'{"state":"on"}', error)
                result = client(transport).set_light("office_desk_lamp", "on")
                self.assertEqual(result["error"], expected)
                self.assertIn(expected, ERRORS)
                self.assertIs(result["ok"], False)
                self.assertNotIn("state", result)
                self.assertNotIn("outcome", result)
                self.assertEqual(transport.gets, [])
                self.assertNotIn(TOKEN, json.dumps(result))
                self.assertNotIn(LAMP, json.dumps(result))

    def test_an_exception_from_the_post_does_not_reach_the_model(self):
        def explode(*_args):
            raise RuntimeError(f"blew up {TOKEN} {LAMP}")

        transport = Transport()
        control = HomeControl(
            URL, TOKEN, {"office_desk_lamp": Light(LAMP)},
            post=explode, fetch=transport.get, pause=lambda _seconds: None,
        )
        text, image = set_light_for_tool(control, {"name": "office_desk_lamp", "action": "on"})
        self.assertIsNone(image)
        parsed = json.loads(text)
        self.assertEqual(parsed["error"], "request_failed")
        self.assertIs(parsed["ok"], False)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(LAMP, text)
        self.assertNotIn("blew up", text)
        self.assertEqual(transport.gets, [])

    def test_unauthorised_names_and_actions_make_no_request(self):
        transport = Transport()
        control = client(transport)
        for action in ("toggle", "turn_on", "turn_off", "brightness", "ON", "on ", "", None, 1):
            with self.subTest(action=action):
                result = control.set_light("office_desk_lamp", action)
                self.assertEqual(result["error"], "unsupported_action")
                self.assertIs(result["ok"], False)
        for name in ("printer_progress", "sensor.pool_temperature", LAMP, "", None):
            with self.subTest(name=name):
                result = control.set_light(name, "on")
                self.assertEqual(result["error"], "unknown_name")
        self.assertEqual(transport.posts, [])
        self.assertEqual(transport.gets, [])

    def test_a_dotted_catalogue_key_cannot_be_called(self):
        transport = Transport()
        control = client(transport, {LAMP: Light(LAMP)})
        self.assertEqual(control.names, [])
        result = control.set_light(LAMP, "on")
        self.assertEqual(result["error"], "unknown_name")
        self.assertEqual(transport.posts, [])

    def test_a_lock_entity_is_not_called_even_if_it_was_forced_into_the_map(self):
        transport = Transport()
        control = client(transport, {"front_door": Light("lock.front_door")})
        result = control.set_light("front_door", "on")
        self.assertEqual(result["error"], "invalid_configuration")
        self.assertEqual(transport.posts, [])
        self.assertNotIn("lock.", json.dumps(result))

    def test_a_missing_hub_makes_no_request(self):
        transport = Transport()
        bad = (
            ("", TOKEN),
            (URL, ""),
            ("homeassistant.local:8123", TOKEN),
            (URL + "/api", TOKEN),
            (f"http://user:{TOKEN}@homeassistant.local:8123", TOKEN),
        )
        for url, token in bad:
            with self.subTest(url=url):
                result = client(transport, url=url, token=token).set_light("office_desk_lamp", "on")
                self.assertEqual(result["error"], "not_configured")
                self.assertNotIn(TOKEN, json.dumps(result))
                self.assertNotIn(TOKEN, repr(client(transport, url=url, token=token)))
        self.assertEqual(transport.posts, [])

    def test_an_empty_catalogue_makes_no_request(self):
        transport = Transport()
        result = client(transport, {}).set_light("office_desk_lamp", "on")
        self.assertEqual(result["error"], "unknown_name")
        self.assertEqual(transport.posts, [])

    def test_ok_is_true_only_when_the_observed_state_equals_the_action(self):
        mismatch = _seal({
            "ok": True,
            "name": "office_desk_lamp",
            "action": "on",
            "outcome": "confirmed",
            "state": "off",
            "entity_id": LAMP,
        })
        self.assertIs(mismatch["ok"], False)
        self.assertNotIn("entity_id", mismatch)
        self.assertNotIn(LAMP, json.dumps(mismatch))
        match = _seal({
            "ok": True,
            "name": "office_desk_lamp",
            "action": "on",
            "outcome": "confirmed",
            "state": "on",
            "entity_id": LAMP,
            "error": "state_unconfirmed",
        })
        self.assertEqual(match, {
            "ok": True,
            "name": "office_desk_lamp",
            "action": "on",
            "outcome": "confirmed",
            "state": "on",
        })


class TransportTests(unittest.TestCase):
    def test_post_discards_the_service_body_and_keeps_the_token_in_the_header(self):
        captured = {}

        class Resp:
            status = 200

            def read(self, n=-1):
                return b'[{"entity_id":"' + LAMP.encode() + b'","state":"on"}]'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["auth"] = req.get_header("Authorization")
            captured["type"] = req.get_header("Content-type")
            captured["data"] = req.data
            captured["timeout"] = timeout
            return Resp()

        with patch("urllib.request.urlopen", fake_urlopen):
            status, body = urllib_post(
                f"{URL}/api/services/switch/turn_on",
                TOKEN,
                json.dumps({"entity_id": LAMP}).encode(),
                5,
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["auth"], "Bearer " + TOKEN)
        self.assertEqual(captured["type"], "application/json")
        self.assertNotIn(TOKEN, captured["url"])
        self.assertEqual(json.loads(captured["data"]), {"entity_id": LAMP})

    def test_post_error_body_is_discarded(self):
        def fake_urlopen(req, timeout=0, context=None):
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(b'{"message":"bad ' + TOKEN.encode() + b" " + LAMP.encode() + b'"}'),
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            status, body = urllib_post(URL + "/api/services/switch/turn_on", TOKEN, b"{}", 5)
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")


class BoundaryTests(unittest.TestCase):
    def test_the_read_module_and_main_stay_free_of_service_calls(self):
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
        main_text = Path(home_assistant.__file__).resolve().parents[0].joinpath("main.py").read_text()
        self.assertNotIn("/api/services", main_text)
        self.assertNotIn("/api/states", main_text)
        self.assertFalse(hasattr(HomeAssistant, "call_service"))
        self.assertFalse(hasattr(HomeAssistant, "set_light"))
        self.assertFalse(hasattr(HomeControl, "call_service"))


class LoopTests(unittest.TestCase):
    def test_a_control_round_is_not_spoken(self):
        seen = []

        def control_light(args):
            seen.append(args)
            return json.dumps({
                "ok": True,
                "name": "office_desk_lamp",
                "action": "on",
                "outcome": "confirmed",
                "state": "on",
            }), None

        brain = thinking.RobotBrain(
            {"control_light": control_light},
            thinking.build_tools([], ["office_desk_lamp"]),
        )
        brain.client.chat.completions.create = _Completions([
            [_chunk(tool_calls=[{
                "index": 0,
                "id": "c",
                "name": "control_light",
                "arguments": '{"name":"office_desk_lamp","action":"on","entity_id":"switch.evil"}',
            }])],
            [_chunk(content="[happy] Lamp is on.")],
        ]).create
        spoken = []
        with patch("sys.stdout", io.StringIO()):
            spoken.extend(brain.reply("protocol question"))
        self.assertEqual(spoken, ["Lamp is on."])
        self.assertEqual(seen, [{
            "name": "office_desk_lamp",
            "action": "on",
            "entity_id": "switch.evil",
        }])
        tool_text = [item["content"] for item in brain.history if item.get("role") == "tool"]
        self.assertEqual(json.loads(tool_text[0])["outcome"], "confirmed")
        self.assertNotIn("switch.", tool_text[0])

    def test_control_and_read_in_one_round_run_in_order(self):
        seen = []

        def control_light(args):
            seen.append(("control_light", args["action"]))
            return json.dumps({"ok": False, "name": "office_desk_lamp", "action": "off", "error": "timeout"}), None

        def get_home_state(args):
            seen.append(("get_home_state", args["name"]))
            return json.dumps({"ok": True, "name": "pool_temperature", "value": 17, "unit": "C"}), None

        brain = thinking.RobotBrain(
            {"control_light": control_light, "get_home_state": get_home_state},
            thinking.build_tools(["pool_temperature"], ["office_desk_lamp"]),
        )
        brain.client.chat.completions.create = _Completions([
            [_chunk(tool_calls=[
                {"index": 0, "id": "c", "name": "control_light", "arguments": '{"name":"office_desk_lamp","action":"off"}'},
                {"index": 1, "id": "g", "name": "get_home_state", "arguments": '{"name":"pool_temperature"}'},
            ])],
            [_chunk(content="Could not confirm the lamp, and the pool is 17.")],
        ]).create
        spoken = []
        with patch("sys.stdout", io.StringIO()):
            spoken.extend(brain.reply("protocol question"))
        self.assertEqual(seen, [("control_light", "off"), ("get_home_state", "pool_temperature")])
        self.assertEqual(spoken, ["Could not confirm the lamp, and the pool is 17."])

    def test_a_hallucinated_control_call_does_not_run_an_action(self):
        def look(_args):
            raise AssertionError("look should not run")

        brain = thinking.RobotBrain(
            {"look": look},
            thinking.build_tools([], []),
        )
        brain.client.chat.completions.create = _Completions([
            [_chunk(tool_calls=[{
                "index": 0,
                "id": "c",
                "name": "control_light",
                "arguments": '{"name":"office_desk_lamp","action":"on"}',
            }])],
            [_chunk(content="I cannot change that.")],
        ]).create
        with patch("sys.stdout", io.StringIO()):
            spoken = list(brain.reply("protocol question"))
        tool_text = [item["content"] for item in brain.history if item.get("role") == "tool"]
        self.assertEqual(tool_text, ["unknown ability control_light"])
        self.assertEqual(spoken, ["I cannot change that."])


class WiringTests(unittest.TestCase):
    def test_the_tool_is_absent_until_a_light_is_configured(self):
        from brain import main

        reader = HomeAssistant(URL, TOKEN, {
            "pool_temperature": Reading("sensor.pool_temperature", True),
        })
        main._home = reader
        main._control = HomeControl(URL, TOKEN, {})
        try:
            tools = [tool["function"]["name"] for tool in main.configured_tools()]
            actions = main.configured_actions()
            self.assertEqual(tools, ["look", "track_face", "get_home_state"])
            self.assertNotIn("control_light", actions)
            self.assertNotIn("control_light", main.ABILITIES)
        finally:
            main._home = None
            main._control = None

    def test_a_configured_light_is_on_the_same_tool_loop(self):
        from brain import main

        transport = Transport()
        transport.push_post(200, b"[]")
        transport.push_get(200, state_body("on"))
        main._home = HomeAssistant(URL, TOKEN, {
            "pool_temperature": Reading("sensor.pool_temperature", True),
        })
        main._control = HomeControl(
            URL, TOKEN, {"office_desk_lamp": Light(LAMP)},
            post=transport.post, fetch=transport.get, pause=lambda _seconds: None,
        )
        try:
            tools = [tool["function"]["name"] for tool in main.configured_tools()]
            actions = main.configured_actions()
            self.assertEqual(tools, ["look", "track_face", "get_home_state", "control_light"])
            self.assertIs(actions["control_light"], main.control_light)
            self.assertNotIn("control_light", main.ABILITIES)
            text, image = actions["control_light"]({"name": "office_desk_lamp", "action": "on"})
        finally:
            main._home = None
            main._control = None
        self.assertIsNone(image)
        self.assertEqual(json.loads(text)["outcome"], "confirmed")
        self.assertNotIn(LAMP, text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(transport.posts[0][0], f"{URL}/api/services/switch/turn_on")


if __name__ == "__main__":
    unittest.main()
