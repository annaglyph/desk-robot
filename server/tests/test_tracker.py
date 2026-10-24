"""The tracker's copy of the firmware's head motion: how long a move takes. No camera."""

import time
import unittest

from brain import config
from brain.tracker import Motion


class MotionTests(unittest.TestCase):
    def test_arrival_time(self):
        m = Motion(config.TRACK_PAN_SPEED, config.TRACK_PAN_ACCEL)  # firmware PAN_MAX_SPEED, PAN_ACCEL
        now = time.time()
        m.t = now
        self.assertEqual(m.arrival_in(now), 0.0)     # already there
        m.set_target(40.0, now)
        t = m.arrival_in(now)
        self.assertGreater(t, 0.4)                   # a 40 degree turn takes about 0.6 s
        self.assertLess(t, 0.8)
        self.assertEqual((m.pos, m.vel), (0.0, 0.0))  # asking doesn't move the copy
        self.assertAlmostEqual(m.at(now + t + 0.001), 40.0)
        self.assertLess(m.at(now + t / 2), 40.0)


if __name__ == "__main__":
    unittest.main()
