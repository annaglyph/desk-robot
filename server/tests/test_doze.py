"""He stays awake while thinking or speaking, including through a goodnight."""

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain import config, main


class StayingUpTests(unittest.TestCase):
    def setUp(self):
        self._thinking = main.thinking
        self._ears = main.ears
        self._awake = main.awake_until
        main.thinking = False
        main.ears = None
        main.awake_until = 0

    def tearDown(self):
        main.thinking = self._thinking
        main.ears = self._ears
        main.awake_until = self._awake

    def test_thinking_blocks_doze_after_the_clock_expires(self):
        main.thinking = True
        main.awake_until = 0
        self.assertTrue(main.staying_up(time.time() + 10))

    def test_muted_audio_blocks_doze_after_the_clock_expires(self):
        ears = SimpleNamespace(muted=threading.Event())
        ears.muted.set()
        main.ears = ears
        main.awake_until = 0
        self.assertTrue(main.staying_up())
        ears.muted.clear()
        self.assertFalse(main.staying_up())

    def test_idle_past_the_clock_may_doze(self):
        main.awake_until = 50
        self.assertTrue(main.staying_up(49))
        self.assertFalse(main.staying_up(50))
        self.assertFalse(main.staying_up(51))

    def test_finished_reply_refreshes_the_clock_before_clearing_thinking(self):
        main.thinking = True
        seen = {}

        def fake_time():
            seen["thinking"] = main.thinking
            return 5_000.0

        with patch("brain.main.time.time", fake_time):
            main.finish_turn(True)
        self.assertTrue(seen["thinking"])
        self.assertFalse(main.thinking)
        self.assertEqual(main.awake_until, 5_000.0 + config.AWAKE_SECONDS)

    def test_interrupted_reply_does_not_refresh_the_clock(self):
        main.thinking = True
        main.awake_until = 12
        main.finish_turn(False)
        self.assertEqual(main.awake_until, 12)
        self.assertFalse(main.thinking)


class DozeLoopTests(unittest.TestCase):
    def setUp(self):
        self._thinking = main.thinking
        self._ears = main.ears
        self._awake = main.awake_until
        main.thinking = False
        main.ears = None
        main.awake_until = 0

    def tearDown(self):
        main.thinking = self._thinking
        main.ears = self._ears
        main.awake_until = self._awake

    def _run_two_checks(self, on_second_sleep) -> list:
        sent = []
        ticks = {"n": 0}

        async def fake_sleep(_seconds):
            ticks["n"] += 1
            if ticks["n"] == 2:
                on_second_sleep()
            if ticks["n"] > 2:
                raise asyncio.CancelledError()

        async def fake_send(payload):
            sent.append(payload)
            return True

        async def fake_held(_held):
            return None

        async def run():
            with (
                patch("brain.main.asyncio.sleep", fake_sleep),
                patch("brain.main.send_to_robot", fake_send),
                patch("brain.main.set_head_held", fake_held),
            ):
                await main.doze_loop()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())
        self.assertGreaterEqual(ticks["n"], 2)
        return sent

    def test_no_doze_while_thinking(self):
        main.thinking = True
        sent = self._run_two_checks(lambda: None)
        self.assertEqual(sent, [])

    def test_doze_once_the_reply_has_finished_and_the_clock_is_expired(self):
        main.thinking = True

        def reply_ends():
            main.thinking = False

        sent = self._run_two_checks(reply_ends)
        self.assertEqual(sent, [{"type": "asleep", "on": True}])


class GoodnightTests(unittest.TestCase):
    def setUp(self):
        self._thinking = main.thinking
        self._awake = main.awake_until
        main.thinking = False
        main.awake_until = time.time() + 30

    def tearDown(self):
        main.thinking = self._thinking
        main.awake_until = self._awake

    def test_goodnight_line_finishes_before_asleep(self):
        events = []

        async def fake_say(line):
            events.append(("say", main.thinking, main.staying_up()))
            self.assertTrue(line)
            return b""

        async def fake_send(payload):
            events.append(("send", payload.get("type"), main.thinking))
            return True

        now = time.time()
        with (
            patch("brain.main.say", fake_say),
            patch("brain.main.send_to_robot", fake_send),
        ):
            asyncio.run(main._handle_heard(("goodnight", now, now, now)))

        kinds = [(kind, payload) for kind, payload, _thinking in events]
        self.assertEqual(kinds, [("send", "emotion"), ("say", True), ("send", "asleep")])
        say = events[1]
        asleep = events[2]
        self.assertTrue(say[1])  # thinking while the line is spoken
        self.assertTrue(say[2])  # doze would not fire during it
        self.assertTrue(asleep[2])  # still thinking when asleep is sent
        self.assertFalse(main.thinking)
        self.assertEqual(main.awake_until, 0.0)


if __name__ == "__main__":
    unittest.main()
