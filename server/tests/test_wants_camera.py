"""A name question is not a request to look. A pointed 'who is that' is."""

import unittest

from brain import main


class WantsCameraTests(unittest.TestCase):
    def test_who_is_anna_does_not_request_a_frame(self):
        self.assertFalse(main.wants_camera("Who is Anna?"))
        self.assertFalse(main.wants_camera("who is Anna"))

    def test_visual_identity_questions_still_request_a_frame(self):
        for question in (
            "Who is that?",
            "Who is this?",
            "Who is there?",
            "Who is in front of you?",
        ):
            self.assertTrue(main.wants_camera(question), question)


if __name__ == "__main__":
    unittest.main()
