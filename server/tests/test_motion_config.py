"""Python neck limits and motion must match the firmware's calibrated config.h."""

import re
import unittest
from pathlib import Path

from brain import config, dance

FW = Path(__file__).resolve().parents[2] / "firmware" / "include" / "config.h"


def _fw_float(name: str) -> float:
    text = FW.read_text()
    match = re.search(rf"constexpr float {name} = (-?[\d.]+)f\s*;", text)
    if not match:
        raise AssertionError(f"{name} not found in {FW}")
    return float(match.group(1))


class MotionConfigTests(unittest.TestCase):
    def test_python_matches_firmware_neck(self):
        self.assertEqual(config.TRACK_PAN_LIMIT, _fw_float("PAN_MAX_DEG"))
        self.assertEqual(-config.TRACK_PAN_LIMIT, _fw_float("PAN_MIN_DEG"))
        self.assertEqual(config.TRACK_TILT_MIN, _fw_float("TILT_MIN_DEG"))
        self.assertEqual(config.TRACK_TILT_MAX, _fw_float("TILT_MAX_DEG"))
        self.assertEqual(config.TRACK_PAN_SPEED, _fw_float("PAN_MAX_SPEED"))
        self.assertEqual(config.TRACK_PAN_ACCEL, _fw_float("PAN_ACCEL"))
        self.assertEqual(config.TRACK_TILT_SPEED, _fw_float("TILT_MAX_SPEED"))
        self.assertEqual(config.TRACK_TILT_ACCEL, _fw_float("TILT_ACCEL"))

    def test_tracking_follow_tilt_stays_inside_travel(self):
        lo, hi = config.TRACK_FOLLOW_TILT
        self.assertGreaterEqual(lo, config.TRACK_TILT_MIN)
        self.assertLessEqual(hi, config.TRACK_TILT_MAX)

    def test_dance_envelope_stays_inside_travel(self):
        self.assertLessEqual(dance.PAN_MAX, config.TRACK_PAN_LIMIT)
        self.assertGreaterEqual(dance.TILT_MIN, config.TRACK_TILT_MIN)
        self.assertLessEqual(dance.TILT_MAX, config.TRACK_TILT_MAX)
        for name, (_, moves) in dance.DANCES.items():
            for pan, tilt, _beats, _face in moves:
                self.assertLessEqual(abs(pan), dance.PAN_MAX, name)
                self.assertGreaterEqual(tilt, dance.TILT_MIN, name)
                self.assertLessEqual(tilt, dance.TILT_MAX, name)
