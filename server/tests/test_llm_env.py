"""LLM_BASE_URL and MODEL come from the environment, with the old defaults."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import config

SERVER = Path(__file__).resolve().parents[1]
EXAMPLE = SERVER / ".env.example"


class EnvTextTests(unittest.TestCase):
    def test_missing_uses_the_default(self):
        self.assertEqual(config.env_text("NOT_A_REAL_DESK_ROBOT_KEY", "fallback"), "fallback")

    def test_blank_and_whitespace_use_the_default(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "", "MODEL": " \t "}):
            self.assertEqual(
                config.env_text("LLM_BASE_URL", config.DEFAULT_LLM_BASE_URL),
                config.DEFAULT_LLM_BASE_URL,
            )
            self.assertEqual(config.env_text("MODEL", config.DEFAULT_MODEL), config.DEFAULT_MODEL)

    def test_override_is_stripped(self):
        with patch.dict(os.environ, {"MODEL": "  openai/gpt-4.1-mini  "}):
            self.assertEqual(config.env_text("MODEL", config.DEFAULT_MODEL), "openai/gpt-4.1-mini")

    def test_defaults_stay_openrouter_and_haiku(self):
        self.assertEqual(config.DEFAULT_LLM_BASE_URL, "https://openrouter.ai/api/v1")
        self.assertEqual(config.DEFAULT_MODEL, "anthropic/claude-haiku-4.5")

    def test_example_documents_the_optional_overrides(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("LLM_BASE_URL=", text)
        self.assertIn("MODEL=", text)
        self.assertIn(config.DEFAULT_LLM_BASE_URL, text)
        self.assertIn(config.DEFAULT_MODEL, text)

    def test_import_applies_override_and_treats_blank_as_unset(self):
        # A value already in the environment wins over server/.env (setdefault),
        # so this does not depend on a developer's private file.
        script = (
            "from brain.config import DEFAULT_LLM_BASE_URL, LLM_BASE_URL, MODEL\n"
            "print(LLM_BASE_URL)\n"
            "print(MODEL)\n"
            "print(DEFAULT_LLM_BASE_URL)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SERVER)
        env["LLM_BASE_URL"] = "   "
        env["MODEL"] = "unit-test-model"
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=SERVER,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        url, model, default = proc.stdout.splitlines()
        self.assertEqual(url, "https://openrouter.ai/api/v1")
        self.assertEqual(model, "unit-test-model")
        self.assertEqual(default, "https://openrouter.ai/api/v1")


if __name__ == "__main__":
    unittest.main()
