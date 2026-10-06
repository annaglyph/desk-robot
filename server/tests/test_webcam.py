"""Which camera a name picks, and that Eyes keeps only the selected one."""

import unittest

from brain.devices import builtin_mic, choose
from brain.eyes import Eyes
from brain.webcam import camera_names, resolve_camera, select_camera


class ResolveCameraTests(unittest.TestCase):
    names = ["FaceTime HD Camera", "Anker PowerConf C200", "OBS Virtual Camera"]

    def test_blank_means_the_first_camera(self):
        self.assertEqual(resolve_camera(self.names, None), 0)
        self.assertEqual(resolve_camera(self.names, ""), 0)

    def test_index_and_unique_substring(self):
        self.assertEqual(resolve_camera(self.names, "1"), 1)
        self.assertEqual(resolve_camera(self.names, "powerconf"), 1)
        self.assertEqual(resolve_camera(self.names, "Anker PowerConf C200"), 1)

    def test_unknown_ambiguous_or_out_of_range(self):
        self.assertIsNone(resolve_camera(self.names, "nope"))
        self.assertIsNone(resolve_camera(self.names, "camera"))  # FaceTime and OBS
        self.assertIsNone(resolve_camera(self.names, "9"))
        self.assertIsNone(resolve_camera([], "PowerConf"))

    def test_index_is_accepted_when_names_cannot_be_listed(self):
        self.assertEqual(resolve_camera([], None), 0)
        self.assertEqual(resolve_camera([], "2"), 2)

    def test_listing_cameras_does_not_raise(self):
        names = camera_names()
        self.assertTrue(all(isinstance(name, str) and name for name in names))


class PreferenceTests(unittest.TestCase):
    cameras = ["FaceTime HD Camera", "Anker PowerConf C200", "OBS Virtual Camera"]
    mics = ["The Matrix Microphone", "MacBook Pro Microphone", "ZoomAudioDevice"]
    docked = "Anker PowerConf C200, FaceTime HD Camera"
    ears = "Anker PowerConf C200, MacBook Pro Microphone"

    def test_anker_wins_when_it_is_plugged_in(self):
        index, missed = select_camera(self.cameras, self.docked)
        self.assertEqual(self.cameras[index], "Anker PowerConf C200")
        self.assertIsNone(missed)
        mics = ["MacBook Pro Microphone", "Anker PowerConf C200"]
        index, missed = choose(mics, self.ears, builtin_mic)
        self.assertEqual(mics[index], "Anker PowerConf C200")
        self.assertIsNone(missed)

    def test_builtin_is_used_when_the_anker_is_absent(self):
        away = ["FaceTime HD Camera", "OBS Virtual Camera"]
        index, missed = select_camera(away, "Anker PowerConf C200")
        self.assertEqual(away[index], "FaceTime HD Camera")
        self.assertEqual(missed, "Anker PowerConf C200")

        index, missed = select_camera(away, self.docked)
        self.assertEqual(away[index], "FaceTime HD Camera")
        self.assertEqual(missed, "Anker PowerConf C200")

        index, missed = choose(self.mics, self.ears, builtin_mic)
        self.assertEqual(self.mics[index], "MacBook Pro Microphone")
        self.assertEqual(missed, "Anker PowerConf C200")

    def test_obs_and_a_phone_are_not_the_builtin(self):
        index, missed = select_camera(
            ["OBS Virtual Camera", "The Matrix Camera", "FaceTime HD Camera"],
            "PowerConf",
        )
        self.assertEqual(index, 2)
        self.assertIn("PowerConf", missed)

        index, _ = choose(self.mics, "PowerConf", builtin_mic)
        self.assertEqual(self.mics[index], "MacBook Pro Microphone")

    def test_empty_setting_leaves_the_choice_to_the_caller(self):
        index, missed = choose(self.mics, None, builtin_mic)
        self.assertIsNone(index)
        self.assertIsNone(missed)
        index, missed = choose(self.mics, "", builtin_mic)
        self.assertIsNone(index)


class EyesSourceTests(unittest.TestCase):
    def test_frames_from_the_other_camera_are_dropped(self):
        eyes = Eyes()
        eyes.push_frame(b"from the mac", source="mac")
        self.assertEqual(eyes.jpeg, b"")
        eyes.push_frame(b"from the robot", source="robot")
        self.assertEqual(eyes.jpeg, b"from the robot")

        eyes.set_source("mac")
        self.assertEqual(eyes.jpeg, b"")  # the robot's picture is not still "now"
        eyes.push_frame(b"from the robot", source="robot")
        self.assertEqual(eyes.jpeg, b"")
        eyes.push_frame(b"from the mac", source="mac")
        self.assertEqual(eyes.current(), (b"from the mac", "mac"))
