"""Fish hears Russ. Everything else still says Rus."""

import json
import os
import unittest
from unittest.mock import patch

from brain import mouth


class _EmptyResponse:
    def read(self, _n):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PronounceTests(unittest.TestCase):
    def test_whole_words_only(self):
        cases = {
            "Rus": "Russ",
            "Hello Rus": "Hello Russ",
            "Hey, Rus!": "Hey, Russ!",
            "Rus's": "Russ's",
            "Rus.": "Russ.",
            "Russia": "Russia",
            "Prussia": "Prussia",
        }
        for original, expected in cases.items():
            self.assertEqual(mouth.pronounce(original), expected, original)

    def test_input_string_is_not_modified(self):
        original = "Hello Rus"
        spoken = mouth.pronounce(original)
        self.assertEqual(spoken, "Hello Russ")
        self.assertEqual(original, "Hello Rus")
        self.assertIsNot(spoken, original)

    def test_fish_receives_the_pronunciation_and_the_reply_stays_rus(self):
        reply = "Hello Rus"
        captured = {}

        def fake_urlopen(req, timeout=30, context=None):
            captured["text"] = json.loads(req.data.decode())["text"]
            return _EmptyResponse()

        with (
            patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "test-key"}),
            patch("brain.mouth.urllib.request.urlopen", fake_urlopen),
        ):
            b"".join(mouth.stream(reply))
        self.assertEqual(reply, "Hello Rus")
        self.assertEqual(mouth.clean_for_tts(reply), "Hello Rus.")
        self.assertEqual(captured["text"], "Hello Russ.")

    def test_fallback_voice_keeps_the_written_name(self):
        reply = "Hello Rus"
        heard = {}

        def fake_builtin(text):
            heard["builtin"] = text
            return b"\x01\x00"

        with (
            patch.object(mouth, "fish_available", return_value=False),
            patch.object(mouth, "_synthesize_builtin", fake_builtin),
        ):
            self.assertTrue(b"".join(mouth.stream(reply)))
        self.assertEqual(reply, "Hello Rus")
        self.assertEqual(heard["builtin"], "Hello Rus.")


if __name__ == "__main__":
    unittest.main()
