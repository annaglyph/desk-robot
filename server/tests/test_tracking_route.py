"""Face-tracking requests reach the model. They are not matched as substrings."""

import asyncio
import time
import unittest
from unittest.mock import patch

from brain import main


PHRASES = [
    "Follow me with your head.",
    "Start following my face.",
    "Stop following me.",
    "Do not track me.",
    "Don't look at me.",
    "You don't need to follow me anymore.",
    "I lost track of the time.",
    "Please follow the steps I wrote down.",
    "Look, that was funny.",
]


class TrackingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tracking = []
        self.asked = []
        self.said = []
        main.awake_until = time.time() + 60

    def _hear(self, text: str) -> None:
        async def fake_set_tracking(on, announce=True):
            self.tracking.append(on)
            return ("tracked", None)

        async def fake_converse(question, ended_at=None, heard_at=None):
            self.asked.append(question)
            return True

        async def fake_say(line):
            self.said.append(line)
            return b""

        async def fake_send(payload):
            return True

        now = time.time()
        with (
            patch.object(main, "set_tracking", fake_set_tracking),
            patch.object(main, "converse", fake_converse),
            patch.object(main, "say", fake_say),
            patch.object(main, "send_to_robot", fake_send),
        ):
            asyncio.run(main._handle_heard((text, now, now, now)))

    def test_tracking_words_reach_the_brain_and_do_not_move_the_head(self):
        for text in PHRASES:
            self.tracking.clear()
            self.asked.clear()
            self._hear(text)
            self.assertEqual(self.tracking, [], text)
            self.assertEqual(self.asked, [text], text)

    def test_sleep_phrase_still_skips_the_brain(self):
        self._hear("goodnight")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.tracking, [])
        self.assertTrue(self.said)


if __name__ == "__main__":
    unittest.main()
