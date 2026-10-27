"""get_home_state reads one allow-listed name through the Home Assistant adapter.

Ported from V1. The model is not called. HTTP is mocked. These tests do not
read a real token.
"""

import io
import json
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace

from brain import config, home, home_assistant, personality, thinking
from brain.home_assistant import HomeAssistant, Reading, state_for_tool

SECRET = "ha-token-do-not-log"
URL = "http://homeassistant.local:8123"

ENTITIES = {
    "pool_temperature": "sensor.pool_temperature",
    "office_temperature": "sensor.office_temperature",
    "printer_progress": "sensor.printer_progress",
    "printer_nozzle_temp": "sensor.printer_nozzle_temperature",
    "printer_bed_temp": "sensor.printer_bed_temperature",
}

# What a live reading of each name looks like. The value is the adapter's,
# not one invented by the tool.
READINGS = {
    "pool_temperature": ("17", "°C", 17),
    "office_temperature": ("23.1", "°C", 23.1),
    "printer_progress": ("63", "%", 63),
    "printer_nozzle_temp": ("220", "°C", 220),
    "printer_bed_temp": ("55", "°C", 55),
}

SAMPLE_NAMES = [
    "living_room_temperature",
    "office_temperature",
    "pool_temperature",
]

MENTIONS = (
    "The pool was lovely yesterday.",
    "I might print something in the office later.",
    "What temperature does water freeze at?",
    "What does a printer nozzle do?",
    "My office lamp is off. What did I just tell you?",
)

READING_LINES = "\n".join(f"{name} = number:{entity}" for name, entity in ENTITIES.items())


def ha_body(state, unit="°C") -> bytes:
    attributes = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return json.dumps({"state": state, "attributes": attributes}).encode()


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


def make_client(fetch, **entities) -> HomeAssistant:
    catalog = {name: Reading(entity, True) for name, entity in ENTITIES.items()}
    catalog.update(entities)
    return HomeAssistant(URL, SECRET, catalog, fetch=fetch)


@contextmanager
def configured(section, token=SECRET, fetch=None):
    """The brain's own controller, configured as the dashboard would, with fake HTTP. Reset afterwards."""
    c = home.controller
    saved = c._fetch
    c._fetch = fetch
    with redirect_stdout(io.StringIO()):
        c.configure(section, token)
    try:
        yield c
    finally:
        c._fetch = saved
        with redirect_stdout(io.StringIO()):
            c.configure({}, "")


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


class _Completions:
    def __init__(self, rounds):
        self.rounds = rounds
        self.seen = []
        self.systems = []
        self.i = 0

    def create(self, **kwargs):
        self.seen.append(kwargs.get("tools"))
        self.systems.append(kwargs["messages"][0]["content"])
        chunks = self.rounds[self.i]
        self.i += 1
        return _Stream(chunks)


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        yield from self._chunks

    def close(self) -> None:
        return None


class _Gate:
    """A stream that counts how many chunks have been pulled, so a test can see whether speech waited."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.released = 0

    def __iter__(self):
        for chunk in self._chunks:
            self.released += 1
            yield chunk

    def close(self) -> None:
        return None


def _speak(actions, rounds, question="protocol question", extra_tools=None, system_prompt=None, quick=None):
    brain = thinking.RobotBrain(actions, extra_tools=extra_tools, system_prompt=system_prompt, quick=quick)
    fake = _Completions(rounds)
    brain.client.chat.completions.create = fake.create
    spoken = []
    logged = io.StringIO()
    with redirect_stdout(logged):
        for sentence in brain.reply(question):
            spoken.append(sentence)
    return brain, fake, " ".join(spoken), logged.getvalue()


def _tool_text(brain) -> list[str]:
    return [item["content"] for item in brain.history if item.get("role") == "tool"]


def home_tool(names=None):
    tools = home.build_tools(SAMPLE_NAMES if names is None else names)
    return tools[-1]["function"]


class SchemaTests(unittest.TestCase):
    def test_only_the_adapter_catalog_is_exposed(self):
        tools = home.build_tools(list(reversed(SAMPLE_NAMES)))
        names = [tool["function"]["name"] for tool in tools]
        self.assertEqual(names, ["get_home_state"])
        self.assertEqual(
            tools[0]["function"]["parameters"]["properties"]["name"]["enum"],
            SAMPLE_NAMES,
        )
        self.assertEqual(home.build_tools([]), [])
        self.assertNotIn("get_home_state", [tool["function"]["name"] for tool in thinking.TOOLS])
        tool = home_tool()
        name = tool["parameters"]["properties"]["name"]
        self.assertEqual(tool["parameters"]["required"], ["name"])
        self.assertEqual(set(tool["parameters"]["properties"]), {"name"})
        self.assertEqual(name["enum"], SAMPLE_NAMES)
        encoded = json.dumps(tool)
        self.assertNotIn("sensor.", encoded)
        self.assertNotIn("desk_lamp", encoded)
        self.assertNotIn("entity_id", encoded)
        self.assertNotIn("printer", encoded)
        for banned in ("control_light", "turn_on", "turn_off", "call_service"):
            self.assertNotIn(banned, names)

    def test_description_keeps_the_rules_and_lists_configured_names(self):
        tool = home_tool()
        text = tool["description"]
        self.assertIn(config.HUMAN_NAME, text)
        self.assertIn("how you learn", text)
        self.assertIn("temperature", text)
        self.assertIn("progressed", text)
        self.assertIn("device is on", text)
        self.assertIn("what is true now", text)
        self.assertIn("You can only read.", text)
        self.assertIn("cannot change a device", text)
        self.assertIn("with this tool", text)
        self.assertNotIn("control_light", text)
        self.assertIn("One semantic name per call.", text)
        self.assertIn("call this tool again for each name", text)
        self.assertIn("merely because a place, device, or topic was mentioned", text)
        self.assertIn(home._device_identity(SAMPLE_NAMES), text)
        self.assertIn('"living room temperature" identifies living_room_temperature.', text)
        self.assertIn('"room temperature" does not identify living_room_temperature.', text)
        self.assertIn(
            "All meaningful words from the listed name, in order, are required to identify it.",
            text,
        )
        self.assertIn("One listed name is not a default.", text)
        self.assertIn("before you answer", text)
        self.assertIn("what was true then", text)
        self.assertIn("not what is true now", text)
        self.assertIn("earlier control result", text)
        self.assertIn("Call this tool again before you state the current value.", text)
        self.assertIn("a fact your human states", text)
        self.assertIn("what was said", text)
        self.assertNotIn("already told you", text)
        self.assertNotIn("is the only truth", text)
        self.assertIn("Never pass a Home Assistant entity id.", text)
        self.assertIn(
            "The listed name the human identified exactly.",
            tool["parameters"]["properties"]["name"]["description"],
        )
        self.assertIn("value and unit", text)
        self.assertIn("Never invent a value.", text)
        self.assertIn("not configured", text)
        self.assertIn("unavailable", text)
        self.assertIn("invalid state", text)
        self.assertIn("authentication failure", text)
        self.assertIn("connection failure", text)
        self.assertIn(
            "Available states: living_room_temperature, office_temperature, pool_temperature.",
            text,
        )
        other = home_tool(["kitchen_humidity"])
        self.assertIn("Available states: kitchen_humidity.", other["description"])
        self.assertNotIn("pool_temperature", other["description"])
        self.assertNotIn("office", other["description"])
        self.assertEqual(other["parameters"]["properties"]["name"]["enum"], ["kitchen_humidity"])


class PromptTests(unittest.TestCase):
    """V1's home rules are added to the prompt only while a home tool is offered, once, in V1's place."""

    RULES = (
        "A live reading from the home is not one of those facts.",
        "`get_home_state` says what is true now.",
        "named exactly",
        "One configured item is not a default.",
        "no reading first",
        "do not call `get_home_state` or `control_light`.",
    )

    def test_the_character_prompt_has_no_home_rules_or_readings(self):
        for prompt in (personality.ROCKY_PROMPT, personality.SYSTEM_PROMPT):
            self.assertNotIn(home.HOME_RULES_HEADING, prompt)
            self.assertNotIn("get_home_state", prompt)
            self.assertNotIn("pool_temperature", prompt)
            self.assertNotIn("HOME_ASSISTANT", prompt)
            self.assertNotIn(SECRET, prompt)

    def test_switched_off_or_nothing_listed_leaves_the_prompt_alone(self):
        base = personality.ROCKY_PROMPT
        for section in ({}, {"enabled": False, "url": URL, "readings": READING_LINES},
                        {"enabled": True, "url": URL, "readings": "", "lights": ""}):
            with self.subTest(section=section), configured(section) as c:
                self.assertEqual(c.system_prompt(base), base)

    def test_rules_are_added_once_in_v1s_place_for_rocky_and_for_a_character(self):
        character = personality._character_prompt({
            "name": "Pip", "prompt": "You are cheerful.", "examples": "[happy] Hi {human}.", "lines": {}})
        with configured({"enabled": True, "url": URL, "readings": READING_LINES}) as c:
            for base in (personality.ROCKY_PROMPT, character):
                with self.subTest(base=base[:20]):
                    prompt = c.system_prompt(base)
                    for rule in self.RULES:
                        self.assertEqual(prompt.count(rule), 1, rule)
                    self.assertIn("from what you know. A live reading from the home is not one of those facts.",
                                  prompt)
                    self.assertLess(prompt.index(home.HOME_RULES_HEADING),
                                    prompt.index("Every reply MUST start with an emotion tag"))
                    self.assertEqual(c.system_prompt(prompt), prompt)   # never twice
                    self.assertNotIn("sensor.", prompt)
                    self.assertNotIn(SECRET, prompt)
                    self.assertNotIn("pool_temperature", prompt)

    def test_the_rules_are_v1s_word_for_word(self):
        from pathlib import Path
        v1 = Path(__file__).resolve().parents[3] / "desk-robot" / "server" / "brain" / "personality.py"
        if not v1.is_file():
            self.skipTest("V1 (desk-robot) is not checked out next to this project")
        source = v1.read_text()
        start = source.index("Home state (history is not the present):")
        block = source[start:source.index("\n\n", start)].replace("\\\n", "")
        self.assertEqual(home.HOME_RULES.strip(), block.strip())
        self.assertIn(home.LIVE_READING_SENTENCE, source.replace("\\\n", ""))

    def test_the_brain_sends_the_rules_only_while_the_tool_is_offered(self):
        fetch = FakeFetch(200, ha_body("17", "°C"))
        rounds = [[_chunk(content="[happy] Hi.")]]
        with configured({"enabled": True, "url": URL, "readings": READING_LINES}, fetch=fetch) as c:
            _brain, fake, _spoken, _log = _speak(home.ACTIONS, rounds, extra_tools=lambda: home.tools(c),
                                                 system_prompt=c.system_prompt)
            self.assertIn(home.HOME_RULES_HEADING, fake.systems[0])
            with redirect_stdout(io.StringIO()):
                c.configure({"enabled": False}, SECRET)
            _brain, fake, _spoken, _log = _speak(home.ACTIONS, [[_chunk(content="[happy] Hi.")]],
                                                 extra_tools=lambda: home.tools(c), system_prompt=c.system_prompt)
            self.assertNotIn(home.HOME_RULES_HEADING, fake.systems[0])
            self.assertEqual([t["function"]["name"] for t in fake.seen[0]],
                             [t["function"]["name"] for t in thinking.TOOLS])


class RoutingTests(unittest.TestCase):
    def test_each_semantic_name_is_what_the_adapter_receives(self):
        for name, (state, unit, value) in READINGS.items():
            with self.subTest(name=name):
                fetch = FakeFetch(200, ha_body(state, unit))
                client = make_client(fetch)
                text, image = state_for_tool(client, {"name": name, "entity_id": "sensor.evil"})
                parsed = json.loads(text)
                self.assertIsNone(image)
                self.assertEqual(parsed, {"ok": True, "name": name, "value": value, "unit": unit})
                self.assertEqual(fetch.calls, [(
                    f"{URL}/api/states/{ENTITIES[name]}",
                    SECRET,
                    home_assistant.TIMEOUT,
                )])
                url = fetch.calls[0][0]
                self.assertNotIn("sensor.evil", url)
                self.assertNotIn(SECRET, url)
                self.assertNotIn(SECRET, text)
                self.assertNotIn("sensor.", text)
                if unit == "°C":
                    self.assertIn("°C", text)
                    self.assertNotIn("\\u00b0", text)

    def test_an_entity_id_is_not_a_semantic_name(self):
        fetch = FakeFetch(200, ha_body("17", "°C"))
        text, image = state_for_tool(make_client(fetch), {"name": "sensor.pool_temperature"})
        self.assertIsNone(image)
        self.assertEqual(json.loads(text), {
            "ok": False,
            "name": "sensor.pool_temperature",
            "error": "unknown_name",
        })
        self.assertEqual(fetch.calls, [])
        self.assertNotIn("value", json.loads(text))

    def test_missing_name_is_an_explicit_failure(self):
        fetch = FakeFetch(200, ha_body("17"))
        for args in ({}, [], None, {"name": None}):
            text, _image = state_for_tool(make_client(fetch), args)
            parsed = json.loads(text)
            self.assertEqual(parsed["ok"], False)
            self.assertEqual(parsed["error"], "unknown_name")
            self.assertNotIn("value", parsed)
            self.assertTrue(text.strip())
        self.assertEqual(fetch.calls, [])


class FailureTests(unittest.TestCase):
    def _client(self, fetch, **entities):
        return make_client(fetch, **entities)

    def test_adapter_failures_are_returned_unchanged(self):
        cases = {
            "not_configured": HomeAssistant("", SECRET, {
                "pool_temperature": Reading("sensor.pool_temperature", True),
            }, fetch=FakeFetch()),
            "entity_not_configured": self._client(
                FakeFetch(), pool_temperature=Reading("", True),
            ),
            "entity_unavailable": self._client(FakeFetch(200, ha_body("unavailable"))),
            "invalid_state": self._client(FakeFetch(200, ha_body("hot"))),
            "authentication_failed": self._client(FakeFetch(401, b"no " + SECRET.encode())),
            "connection_failed": self._client(FakeFetch(
                error=urllib.error.URLError(OSError(f"refused {SECRET}")),
            )),
        }
        self.assertEqual(
            set(cases),
            {
                "not_configured",
                "entity_not_configured",
                "entity_unavailable",
                "invalid_state",
                "authentication_failed",
                "connection_failed",
            },
        )
        for error, client in cases.items():
            with self.subTest(error=error):
                text, image = state_for_tool(client, {"name": "pool_temperature"})
                parsed = json.loads(text)
                self.assertIsNone(image)
                self.assertEqual(parsed, client.get_state("pool_temperature"))
                self.assertEqual(parsed, {
                    "ok": False,
                    "name": "pool_temperature",
                    "error": error,
                })
                self.assertNotIn("value", parsed)
                self.assertNotRegex(text, r"\d")
                self.assertNotIn(SECRET, text)
                self.assertNotIn("refused", text)
                self.assertNotEqual(text.strip(), "")


class BrainTests(unittest.TestCase):
    def _actions(self, client, seen):
        def get_home_state(args):
            seen.append(args.get("name"))
            return state_for_tool(client, args)

        def look(args):
            seen.append(("look", args.get("direction")))
            return ("Head is now at pan 0 deg (center).", None)

        def track_face(args):
            seen.append(("track_face", args.get("on")))
            return ("now following the human's face" if args.get("on") else "stopped following", None)

        return {"get_home_state": get_home_state, "look": look, "track_face": track_face}

    def test_each_name_is_returned_to_the_model(self):
        fetch = _Router()
        client = make_client(fetch)
        seen = []
        calls = [
            {
                "index": index,
                "id": f"h{index}",
                "name": "get_home_state",
                "arguments": json.dumps({"name": name}),
            }
            for index, name in enumerate(READINGS)
        ]
        brain, fake, spoken, log = _speak(
            self._actions(client, seen),
            [
                [_chunk(tool_calls=calls)],
                [_chunk(content="[neutral] Read them.")],
            ],
            question="What's the pool temperature, the office, the print, the nozzle, and the bed?",
            extra_tools=lambda: home.build_tools(client.names),
        )
        self.assertEqual(seen, list(READINGS))
        self.assertEqual(spoken, "Read them.")
        returned = [json.loads(item) for item in _tool_text(brain)]
        self.assertEqual(len(returned), 5)
        for name, (_state, unit, value) in READINGS.items():
            match = next(item for item in returned if item["name"] == name)
            self.assertEqual(match, {"ok": True, "name": name, "value": value, "unit": unit})
        self.assertEqual(len(fetch.calls), 5)
        for url, _token, _timeout in fetch.calls:
            self.assertNotIn(SECRET, url)
        self.assertNotIn(SECRET, log)
        self.assertNotIn(SECRET, spoken)
        self.assertNotIn("Camera view after moving", str(brain.history))
        expected = [*thinking.TOOLS, *home.build_tools(client.names)]
        self.assertEqual(fake.seen, [expected, expected])

    def test_failures_reach_the_model_with_no_invented_value(self):
        cases = {
            "not_configured": HomeAssistant("", SECRET, {
                "pool_temperature": Reading("sensor.pool_temperature", True),
            }, fetch=FakeFetch()),
            "entity_unavailable": make_client(FakeFetch(200, ha_body("unavailable"))),
            "invalid_state": make_client(FakeFetch(200, ha_body("unknown"))),
            "authentication_failed": make_client(FakeFetch(401, b"")),
            "connection_failed": make_client(FakeFetch(error=urllib.error.URLError("down"))),
        }
        for error, client in cases.items():
            with self.subTest(error=error):
                seen = []
                brain, _fake, spoken, log = _speak(
                    self._actions(client, seen),
                    [[
                        _chunk(tool_calls=[{
                            "index": 0,
                            "id": "h",
                            "name": "get_home_state",
                            "arguments": '{"name": "pool_temperature"}',
                        }]),
                    ], [
                        _chunk(content="[sad] Could not read it."),
                    ]],
                    question="What's the pool temperature?",
                )
                self.assertEqual(seen, ["pool_temperature"])
                self.assertEqual(_tool_text(brain), [json.dumps({
                    "ok": False,
                    "name": "pool_temperature",
                    "error": error,
                }, ensure_ascii=False)])
                self.assertNotIn("value", _tool_text(brain)[0])
                self.assertNotRegex(_tool_text(brain)[0], r"\d")
                self.assertEqual(spoken, "Could not read it.")
                self.assertNotRegex(spoken, r"\d")
                self.assertNotIn(SECRET, log)
                self.assertNotIn(SECRET, spoken)

    def _home_tools(self):
        return lambda: home.build_tools(["pool_temperature"], ["office_desk_lamp"])

    def test_a_home_call_keeps_its_lead_in_but_does_not_speak_it(self):
        """Narration written before get_home_state stays in the transcript and is not spoken. The answer after
        the result is spoken once."""
        fetch = FakeFetch(200, ha_body("off"))
        client = make_client(fetch)
        seen = []
        lead = "The human asks about the office desk lamp by its exact name, so I read its current state."
        brain, fake, spoken, _log = _speak(
            self._actions(client, seen),
            [[
                _chunk(content=f"[neutral] {lead} "),
                _chunk(tool_calls=[{
                    "index": 0, "id": "lamp", "name": "get_home_state",
                    "arguments": '{"name": "pool_temperature"}',
                }]),
            ], [
                _chunk(content="[neutral] The office desk lamp is off, Rus. Want me to turn it on?"),
            ]],
            question="Is the office desk lamp on or off?",
            extra_tools=self._home_tools(),
        )
        self.assertEqual(seen, ["pool_temperature"])
        self.assertEqual(spoken, "The office desk lamp is off, Rus. Want me to turn it on?")
        self.assertNotIn("exact name", spoken)
        self.assertEqual(spoken.count("The office desk lamp is off"), 1)
        transcript = next(item["content"] for item in brain.history if item.get("tool_calls"))
        self.assertIn(lead, transcript)
        self.assertEqual(fake.i, 2)

    def test_a_light_call_and_a_mixed_round_do_not_speak_the_lead_in(self):
        fetch = FakeFetch(200, ha_body("on"))
        client = make_client(fetch)
        seen = []
        actions = self._actions(client, seen)

        def control_light(args):
            seen.append(("control_light", args.get("action")))
            return ('{"ok": true, "outcome": "confirmed", "state": "on"}', None)

        def look(args):
            seen.append(("look", args.get("direction")))
            return ("Head is now at pan -40.", None, True)

        actions = {**actions, "control_light": control_light, "look": look}
        brain, _fake, spoken, _log = _speak(
            actions,
            [[
                _chunk(content="[neutral] I will call both tools now. "),
                _chunk(tool_calls=[
                    {"index": 0, "id": "c", "name": "control_light", "arguments": '{"name":"office_desk_lamp","action":"on"}'},
                    {"index": 1, "id": "l", "name": "look", "arguments": '{"direction":"left"}'},
                ]),
            ], [
                _chunk(content="[happy] The office desk lamp is on."),
            ]],
            extra_tools=self._home_tools(),
        )
        self.assertEqual(seen[:2], [("control_light", "on"), ("look", "left")])
        self.assertEqual(spoken, "The office desk lamp is on.")
        self.assertNotIn("both tools", spoken)
        self.assertIn("both tools", next(item["content"] for item in brain.history if item.get("tool_calls")))

    def test_an_ordinary_answer_is_spoken_once_while_home_tools_are_offered(self):
        seen = []
        _brain, fake, spoken, _log = _speak(
            self._actions(make_client(FakeFetch()), seen),
            [[_chunk(content="[happy] Yes yes yes. Good day.")]],
            question="What is a weekend?",
            extra_tools=self._home_tools(),
        )
        self.assertEqual(seen, [])
        self.assertEqual(spoken, "Yes yes yes. Good day.")
        self.assertEqual(fake.i, 1)

    def test_a_quick_action_still_speaks_its_lead_in_while_home_tools_are_offered(self):
        seen = []

        def look(args):
            seen.append(args.get("direction"))
            return ("Head is now at pan -40.", None, True)

        def track_face(args):
            seen.append(args.get("on"))
            return ("now following the human's face", None, True)

        def dance(_args):
            seen.append("dance")
            return ("That was the robot.", None, True)

        def search(_args):
            seen.append("search")
            return ("opened", None)

        for name, action, line, arguments in (
            ("look", look, "Turning left.", '{"direction":"left"}'),
            ("track_face", track_face, "Following you.", '{"on": true}'),
            ("dance", dance, "Dancing now.", "{}"),
            ("search_web", search, "Opening.", "{}"),
        ):
            with self.subTest(name=name):
                seen.clear()
                _brain, fake, spoken, _log = _speak(
                    {name: action, "get_home_state": lambda a: ("{}", None)},
                    [[
                        _chunk(content=f"[happy] {line} "),
                        _chunk(tool_calls=[{"index": 0, "id": "q", "name": name, "arguments": arguments}]),
                    ]],
                    extra_tools=self._home_tools() if name == "look" else (
                        lambda: [*self._home_tools()(), {"type": "function", "function": {"name": "search_web"}}]),
                    # search_web ends the reply because it is quick; look ends it because its result says it is done
                    quick={"search_web"} if name == "search_web" else None,
                )
                self.assertEqual(spoken, line)
                self.assertEqual(fake.i, 1)
                self.assertTrue(seen)

    def test_without_home_tools_the_first_sentence_is_spoken_before_the_round_ends(self):
        """Home tools being unavailable keeps streaming: the first sentence is out before the rest is read."""
        gate = _Gate([
            _chunk(content="[happy] Hello there. "),
            _chunk(content="And that is all."),
        ])
        brain = thinking.RobotBrain({"look": lambda a: ("ok", None)})
        brain.client.chat.completions.create = lambda **_kw: gate
        sentences = brain.reply("hello")
        self.assertEqual(next(sentences), "Hello there.")
        self.assertEqual(gate.released, 1)
        self.assertEqual(list(sentences), ["And that is all."])
        self.assertEqual(gate.released, 2)

    def test_with_home_tools_an_ordinary_answer_waits_until_the_tool_decision(self):
        """An unrelated question is still spoken in full, but only after the round shows there is no home call."""
        gate = _Gate([
            _chunk(content="[happy] Hello there. "),
            _chunk(content="And that is all."),
        ])
        brain = thinking.RobotBrain(
            {"get_home_state": lambda a: ("{}", None)},
            extra_tools=self._home_tools(),
        )
        brain.client.chat.completions.create = lambda **_kw: gate
        sentences = brain.reply("hello")
        self.assertEqual(next(sentences), "Hello there.")
        self.assertEqual(gate.released, 2)
        self.assertEqual(list(sentences), ["And that is all."])

    def test_a_mention_does_not_call_the_tool(self):
        for prompt in MENTIONS:
            with self.subTest(prompt=prompt):
                seen = []
                fetch = FakeFetch(200, ha_body("17", "°C"))
                _brain, _fake, spoken, log = _speak(
                    self._actions(make_client(fetch), seen),
                    [[_chunk(content="[happy] Yes yes yes. Good day.")]],
                    question=prompt,
                )
                self.assertEqual(seen, [])
                self.assertEqual(fetch.calls, [])
                self.assertEqual(spoken, "Yes yes yes. Good day.")
                self.assertNotIn(SECRET, log)

    def test_malformed_arguments_are_not_a_blank_result(self):
        fetch = FakeFetch(200, ha_body("17", "°C"))
        seen = []
        brain, _fake, spoken, _log = _speak(
            self._actions(make_client(fetch), seen),
            [[
                _chunk(tool_calls=[{
                    "index": 0,
                    "id": "h",
                    "name": "get_home_state",
                    "arguments": "{",
                }]),
            ], [
                _chunk(content="[sad] Could not read it."),
            ]],
        )
        self.assertEqual(seen, [None])
        self.assertEqual(json.loads(_tool_text(brain)[0]), {
            "ok": False,
            "name": "",
            "error": "unknown_name",
        })
        self.assertEqual(fetch.calls, [])
        self.assertEqual(spoken, "Could not read it.")

    def test_several_readings_in_one_turn_and_look_and_track_face_still_run(self):
        fetch = _Router()
        client = make_client(fetch)
        seen = []
        brain, fake, spoken, log = _speak(
            self._actions(client, seen),
            [[
                _chunk(tool_calls=[{"index": 2, "id": "t", "name": "track_face", "arguments": '{"on": true}'}]),
                _chunk(tool_calls=[{"index": 0, "id": "o", "name": "get_home_state", "arguments": '{"name":"office_temperature"}'}]),
                _chunk(tool_calls=[{"index": 1, "id": "l", "name": "look", "arguments": '{"direction":"left"}'}]),
                _chunk(tool_calls=[{"index": 3, "id": "p", "name": "get_home_state", "arguments": '{"name":"pool_temperature"}'}]),
            ], [
                _chunk(tool_calls=[{
                    "index": 0,
                    "id": "r",
                    "name": "get_home_state",
                    "arguments": '{"name":"printer_progress"}',
                }]),
            ], [
                _chunk(content="[neutral] Office, pool, and the print."),
            ]],
            question="Is the office warmer than the pool, and how far through is the print? Also look left and follow me.",
        )
        self.assertEqual(seen, [
            "office_temperature",
            ("look", "left"),
            ("track_face", True),
            "pool_temperature",
            "printer_progress",
        ])
        returned = [json.loads(item) for item in _tool_text(brain) if item.startswith("{")]
        self.assertEqual([item["name"] for item in returned], [
            "office_temperature",
            "pool_temperature",
            "printer_progress",
        ])
        by_name = {item["name"]: item for item in returned}
        self.assertEqual(by_name["office_temperature"]["value"], 23.1)
        self.assertEqual(by_name["pool_temperature"]["value"], 17)
        self.assertEqual(by_name["printer_progress"]["value"], 63)
        self.assertEqual(by_name["printer_progress"]["unit"], "%")
        self.assertIn("Head is now at pan 0 deg (center).", _tool_text(brain))
        self.assertIn("now following the human's face", _tool_text(brain))
        self.assertEqual(spoken, "Office, pool, and the print.")
        self.assertEqual(len(fake.seen), 3)
        self.assertNotIn(SECRET, log)
        self.assertNotIn("sensor.", " ".join(_tool_text(brain)))


class _Router:
    def __init__(self):
        self.calls = []

    def __call__(self, url, token, timeout):
        self.calls.append((url, token, timeout))
        for name, entity in ENTITIES.items():
            if url.endswith("/api/states/" + entity):
                state, unit, _value = READINGS[name]
                return 200, ha_body(state, unit)
        raise AssertionError(url)


class AbilityTests(unittest.TestCase):
    def test_production_ability_reads_fresh_through_the_current_catalogue(self):
        from brain import main

        fetch = FakeFetch(200, ha_body("17", "°C"))
        with configured({"enabled": True, "url": URL, "readings": READING_LINES}, fetch=fetch) as c:
            first, image = main.ABILITIES["get_home_state"]({"name": "pool_temperature"})
            second, _image = main.ABILITIES["get_home_state"]({"name": "office_temperature"})
            third, _image = main.ABILITIES["get_home_state"]({"name": "pool_temperature"})
            advertised = home.tools(c)
        names = [tool["function"]["name"] for tool in advertised]
        self.assertEqual(names, ["get_home_state"])
        enum = advertised[0]["function"]["parameters"]["properties"]["name"]["enum"]
        self.assertEqual(enum, sorted(ENTITIES))
        self.assertNotIn("sensor.", json.dumps(advertised))
        self.assertNotIn(SECRET, json.dumps(advertised))
        self.assertIs(main.ABILITIES["get_home_state"], home.ACTIONS["get_home_state"])
        self.assertIn("look", main.ABILITIES)
        self.assertIn("track_face", main.ABILITIES)
        self.assertIsNone(image)
        self.assertEqual(json.loads(first)["value"], 17)
        self.assertEqual(json.loads(first)["name"], "pool_temperature")
        self.assertEqual(json.loads(second), {
            "ok": True,
            "name": "office_temperature",
            "value": 17,
            "unit": "°C",
        })
        # every question is a fresh GET: nothing is cached between calls
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _token, _timeout in fetch.calls],
            ["sensor.pool_temperature", "sensor.office_temperature", "sensor.pool_temperature"],
        )
        for text in (first, second, third):
            self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main()
