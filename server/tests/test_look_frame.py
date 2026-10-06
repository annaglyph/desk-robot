"""look waits a bounded time for a frame newer than the one before the move."""

import asyncio
import time
import unittest
from unittest.mock import patch

from brain import main


class LookFrameTests(unittest.TestCase):
    def setUp(self):
        self.eyes = main.eyes
        self._saved = (
            self.eyes.jpeg,
            self.eyes.source,
            self.eyes.frame_seq,
            self.eyes.frame_at,
            self.eyes.frames,
        )
        self._tracker = main.tracker
        main.tracker = None
        self.sent = []

    def tearDown(self):
        jpeg, source, seq, at, frames = self._saved
        self.eyes.jpeg = jpeg
        self.eyes.source = source
        self.eyes.frame_seq = seq
        self.eyes.frame_at = at
        self.eyes.frames = frames
        main.tracker = self._tracker

    def _look(self, args, *, source="robot", jpeg=b"", seq=4, during_settle=None, on_wait=None):
        self.eyes.source = source
        self.eyes.jpeg = jpeg
        self.eyes.frame_seq = seq
        self.eyes.frame_at = time.time() if jpeg else 0.0
        self.sent.clear()
        clock = {"t": 1_000.0}
        slept = []
        waits = []

        def mono():
            return clock["t"]

        async def fake_sleep(seconds):
            slept.append(seconds)
            clock["t"] += seconds
            if during_settle is not None:
                during_settle()

        def fake_wait(seen, timeout):
            waits.append((seen, timeout))
            if on_wait is not None:
                on_wait()
            return self.eyes.frame_seq

        async def fake_send(payload):
            self.sent.append(payload)
            return True

        async def fake_held(_held):
            return None

        async def run():
            with (
                patch("brain.main.time.monotonic", mono),
                patch("brain.main.asyncio.sleep", fake_sleep),
                patch.object(self.eyes, "wait_for_new", fake_wait),
                patch("brain.main.send_to_robot", fake_send),
                patch("brain.main.set_head_held", fake_held),
            ):
                return await main.look(args)

        text, image = asyncio.run(run())
        return text, image, slept, waits

    def test_new_frame_after_the_move_is_fresh(self):
        def arrived():
            self.eyes.push_frame(b"new-view", source="robot")

        text, image, slept, waits = self._look({"direction": "left"}, on_wait=arrived)
        self.assertIn("Fresh camera image attached.", text)
        self.assertNotIn("older one", text)
        self.assertEqual(image, b"new-view")
        self.assertEqual(self.sent, [{"type": "pan", "deg": -40.0}])
        self.assertEqual(slept, [main.LOOK_SETTLE_SECONDS])
        seen, timeout = waits[0]
        self.assertEqual(seen, 4)
        self.assertAlmostEqual(timeout, main.LOOK_WAIT_BUDGET - main.LOOK_SETTLE_SECONDS)
        self.assertLessEqual(slept[0] + timeout, main.LOOK_WAIT_BUDGET + 1e-9)

    def test_frame_that_arrives_during_settle_is_still_newer(self):
        def arrived():
            self.eyes.push_frame(b"settled", source="robot")

        text, image, _slept, waits = self._look({"direction": "right", "degrees": 15}, during_settle=arrived)
        self.assertIn("Fresh camera image attached.", text)
        self.assertEqual(image, b"settled")
        self.assertEqual(self.sent, [{"type": "pan", "deg": 15.0}])
        self.assertEqual(waits[0][0], 4)

    def test_timeout_keeps_an_older_frame_and_does_not_call_it_fresh(self):
        text, image, slept, waits = self._look({"direction": "down"}, jpeg=b"already-there")
        self.assertIn("older one", text)
        self.assertNotIn("Fresh camera image", text)
        self.assertIn("No new camera frame", text)
        self.assertEqual(image, b"already-there")
        self.assertEqual(self.sent, [{"type": "tilt", "deg": -30.0}])
        self.assertAlmostEqual(waits[0][1], main.LOOK_WAIT_BUDGET - slept[0])
        self.assertGreater(waits[0][1], 0)
        self.assertLessEqual(waits[0][1], main.LOOK_WAIT_BUDGET)

    def test_timeout_with_nothing_in_date_says_no_image(self):
        text, image, _slept, waits = self._look({"direction": "center"})
        self.assertIn("No camera image available.", text)
        self.assertNotIn("Fresh camera image", text)
        self.assertNotIn("older one", text)
        self.assertIsNone(image)
        self.assertEqual(self.sent, [
            {"type": "pan", "deg": 0.0},
            {"type": "tilt", "deg": 0.0},
        ])
        self.assertGreater(waits[0][1], 0)

    def test_mac_webcam_does_not_move_or_wait(self):
        text, image, slept, waits = self._look(
            {"direction": "left"}, source="mac", jpeg=b"desk-cam",
        )
        self.assertIn("does not move", text)
        self.assertIn("Fresh camera image attached.", text)
        self.assertEqual(image, b"desk-cam")
        self.assertEqual(self.sent, [])
        self.assertEqual(slept, [])
        self.assertEqual(waits, [])

    def test_mac_webcam_without_a_frame_does_not_wait(self):
        text, image, slept, waits = self._look({"direction": "left"}, source="mac")
        self.assertIn("No camera image available.", text)
        self.assertNotIn("Fresh camera image", text)
        self.assertIsNone(image)
        self.assertEqual(slept, [])
        self.assertEqual(waits, [])


if __name__ == "__main__":
    unittest.main()
