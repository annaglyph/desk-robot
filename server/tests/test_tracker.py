"""Tracking adds the latest face error once per send, not once per frame."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain import config, tracker as tracker_mod
from brain.tracker import SEND_INTERVAL, Tracker


def face_at(ex: float, ey: float, w: int = 320, h: int = 240, size: int = 40):
    """A box whose centre is `ex`/`ey` in the tracker's normalised frame."""
    cx = (w / 2) * (1.0 + ex)
    cy = (h / 2) * (1.0 + ey)
    return (int(cx - size / 2), int(cy - size / 2), size, size)


def tilt_step(ey: float) -> float:
    return config.TRACK_TILT_SIGN * (-ey) * (config.TRACK_VFOV / 2) * config.TRACK_GAIN


class IntegrateOncePerSendTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.eyes = SimpleNamespace(source="robot", frame_at=1.0)
        self.tr = Tracker(self.eyes, lambda p, t, on, gen: self.sent.append((p, t, on, gen)))

    def _steer(self, ex, ey, now):
        self.tr._steer(face_at(ex, ey), 320, 240, now)

    def test_two_identical_errors_50ms_apart_integrate_once(self):
        ey = 0.5
        self.assertGreater(abs(ey), config.TRACK_DEADBAND)
        expected = tilt_step(ey)
        with patch.object(tracker_mod, "TRACK_DEBUG", False):
            self._steer(0.0, ey, 1.00)
            self._steer(0.0, ey, 1.00 + 0.050)
        self.assertAlmostEqual(self.tr.tilt, expected)
        self.assertEqual(len(self.sent), 1)
        self.assertAlmostEqual(self.sent[0][1], round(expected, 1))

    def test_a_later_send_uses_the_latest_error_not_a_sum_of_frames(self):
        ey = 0.5
        step = tilt_step(ey)
        with patch.object(tracker_mod, "TRACK_DEBUG", False):
            self._steer(0.0, ey, 1.00)
            self._steer(0.0, ey, 1.00 + 0.050)  # inside the send interval; ignored
            self._steer(0.0, ey, 1.00 + SEND_INTERVAL + 0.01)
        self.assertAlmostEqual(self.tr.tilt, 2 * step)
        self.assertEqual(len(self.sent), 2)

    def test_deadband_frame_does_not_nudge_the_estimate(self):
        with patch.object(tracker_mod, "TRACK_DEBUG", False):
            self._steer(0.0, config.TRACK_DEADBAND * 0.5, 1.00)
        self.assertEqual(self.tr.tilt, 0.0)
        self.assertEqual(self.sent[0][1], 0.0)  # became_tracking still announces


if __name__ == "__main__":
    unittest.main()
