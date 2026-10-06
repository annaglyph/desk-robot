"""Tool rounds stay silent, and a later round may still call a tool."""

import unittest
from types import SimpleNamespace

import openai

from brain import config, thinking


def _chunk(content=None, tool_calls=None, choices=True):
    if not choices:
        return SimpleNamespace(choices=[])
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
        self.seen = []
        self.i = 0

    def create(self, **kwargs):
        self.seen.append(kwargs.get("tools"))
        if self.i >= len(self.rounds):
            raise AssertionError(f"model called {self.i + 1} times; scripted {len(self.rounds)}")
        chunks = self.rounds[self.i]
        self.i += 1
        return _Stream(chunks)


def _speak(actions, rounds, on_emotion=None) -> tuple[thinking.RobotBrain, _Completions, str]:
    brain = thinking.RobotBrain(actions)
    fake = _Completions(rounds)
    brain.client.chat.completions.create = fake.create
    spoken = []
    for sentence in brain.reply("protocol question", on_emotion=on_emotion):
        spoken.append(sentence)
    return brain, fake, " ".join(spoken)


class LookDescriptionTests(unittest.TestCase):
    def test_a_mentioned_object_is_not_enough_reason_to_look(self):
        look = thinking.TOOLS[0]["function"]
        text = look["description"]
        self.assertIn("explicitly asked to look, turn, inspect, or check visually", text)
        self.assertIn("genuinely needs visual information", text)
        self.assertIn("later turn", text)
        self.assertIn("person, physical object, room, or past event", text)
        self.assertIn("even if you think your head is already there", text)
        self.assertIn("left and right", text)
        self.assertIn("down", text)
        self.assertIn("level", text)
        self.assertIn("center", text)
        self.assertNotIn("whenever", text.lower())
        self.assertEqual(
            look["parameters"]["properties"]["direction"]["enum"],
            ["left", "right", "down", "level", "center"],
        )

    def test_track_face_is_unchanged(self):
        track = thinking.TOOLS[1]["function"]
        self.assertEqual(track["name"], "track_face")
        self.assertEqual(track["description"], "Start or stop following the human's face with your head.")
        self.assertEqual(track["parameters"]["required"], ["on"])
        self.assertEqual(track["parameters"]["properties"]["on"], {"type": "boolean"})


class ToolRoundTests(unittest.TestCase):
    def test_tool_call_after_three_sentences_still_runs_and_the_lead_in_is_silent(self):
        seen = []

        def look(args):
            seen.append(args.get("direction"))
            return ("moved", None)

        emotions = []
        _brain, fake, spoken = _speak(
            {"look": look},
            [[
                _chunk(content="[happy] One. Two. Three. Four. "),
                _chunk(tool_calls=[{"index": 0, "id": "late", "name": "look", "arguments": '{"direction":"left"}'}]),
            ], [
                _chunk(content="[neutral] Red square."),
            ]],
            on_emotion=emotions.append,
        )
        self.assertEqual(seen, ["left"])
        self.assertNotIn("One", spoken)
        self.assertNotIn("Let me", spoken)
        self.assertNotIn("Four", spoken)
        self.assertIn("Red square", spoken)
        self.assertNotIn("[neutral]", spoken)
        self.assertNotIn("[happy]", spoken)
        self.assertEqual(fake.seen, [thinking.TOOLS, thinking.TOOLS])
        self.assertEqual(emotions[0], "happy")

    def test_later_known_emotion_tags_are_not_spoken(self):
        emotions = []
        _brain, _fake, spoken = _speak(
            {"look": lambda args: ("moved", None)},
            [[_chunk(content="[thinking] General knowledge. [happy] Yellow. [note] Keep this.")]],
            on_emotion=emotions.append,
        )
        self.assertEqual(spoken, "General knowledge. Yellow. [note] Keep this.")
        self.assertNotIn("[thinking]", spoken)
        self.assertNotIn("[happy]", spoken)
        self.assertIn("[note]", spoken)
        self.assertEqual(emotions[0], "thinking")

    def test_several_tools_in_one_round_run_in_index_order(self):
        seen = []

        def look(args):
            seen.append(("look", args.get("direction")))
            return ("moved", b"img" if args.get("direction") == "right" else None)

        def track(args):
            seen.append(("track_face", args.get("on")))
            return ("now following the human's face", None)

        brain, _fake, spoken = _speak(
            {"look": look, "track_face": track},
            [[
                _chunk(tool_calls=[{"index": 1, "id": "t", "name": "track_face", "arguments": '{"on": true}'}]),
                _chunk(tool_calls=[{"index": 0, "id": "l", "name": "look", "arguments": '{"direction":"left"}'}]),
                _chunk(tool_calls=[{"index": 2, "id": "l2", "name": "look", "arguments": '{"direction":"right"}'}]),
            ], [
                _chunk(content="[neutral] Done."),
            ]],
        )
        self.assertEqual(seen, [("look", "left"), ("track_face", True), ("look", "right")])
        self.assertEqual(spoken, "Done.")
        self.assertIn("a camera image was attached here", str(brain.history))

    def test_a_later_round_may_call_another_tool(self):
        seen = []

        def look(args):
            seen.append(args.get("direction"))
            return ("moved", None)

        _brain, fake, spoken = _speak(
            {"look": look},
            [
                [_chunk(content="[thinking] Let me look.", tool_calls=[
                    {"index": 0, "id": "c1", "name": "look", "arguments": '{"direction":"center"}'},
                ])],
                [_chunk(tool_calls=[
                    {"index": 0, "id": "c2", "name": "look", "arguments": '{"direction":"down"}'},
                ])],
                [_chunk(content="[surprised] Now I see it.")],
            ],
        )
        self.assertEqual(seen, ["center", "down"])
        self.assertNotIn("Let me look", spoken)
        self.assertEqual(spoken, "Now I see it.")
        self.assertEqual(fake.seen, [thinking.TOOLS, thinking.TOOLS, thinking.TOOLS])
        self.assertIsNot(fake.seen[0], openai.NOT_GIVEN)

    def test_three_sentence_cap_applies_after_the_stream_when_there_is_no_tool(self):
        _brain, _fake, spoken = _speak(
            {},
            [[_chunk(content="[happy] One. Two. Three. Four. ")]],
        )
        self.assertIn("One.", spoken)
        self.assertIn("Two.", spoken)
        self.assertIn("Three.", spoken)
        self.assertNotIn("Four", spoken)
        self.assertEqual(config.REPLY_MAX_SENTENCES, 3)


if __name__ == "__main__":
    unittest.main()
