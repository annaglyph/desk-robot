"""Wi-Fi profile migrate/select rules (firmware/src/net_policy.h), compiled on the host."""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "test" / "net_policy_host.cpp"


class WifiProfilePolicyTests(unittest.TestCase):
    def test_host_policy_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "net_policy_host"
            subprocess.check_call(
                ["c++", "-std=c++17", "-Wall", "-Werror", "-o", str(out), str(SRC)],
            )
            got = subprocess.check_output([str(out)], text=True)
        self.assertEqual(got.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
