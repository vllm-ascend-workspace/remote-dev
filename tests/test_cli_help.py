from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE_DEV = ROOT / ".remote-dev"
if str(REMOTE_DEV) not in sys.path:
    sys.path.insert(0, str(REMOTE_DEV))

from mcp.schemas import TOOL_SCHEMAS  # noqa: E402


class CliHelpTests(unittest.TestCase):
    def test_cli_wrappers_have_help(self) -> None:
        scripts = sorted((ROOT / ".remote-dev" / "tools").glob("remote_*.py"))
        expected_scripts = {ROOT / ".remote-dev" / "tools" / (name.replace(".", "_") + ".py") for name in TOOL_SCHEMAS}
        self.assertEqual(set(scripts), expected_scripts)
        for script_path in scripts:
            script = str(script_path.relative_to(ROOT))
            with self.subTest(script=script):
                proc = subprocess.run([sys.executable, str(script_path), "--help"], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_claude_skill_mirror_check_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / ".remote-dev" / "tools" / "sync_claude_skills.py"), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_cli_errors_return_result_contract_without_traceback(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / ".remote-dev" / "tools" / "remote_job_status.py"),
                "--job-id",
                "job-does-not-exist",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "remote.job_status")
        self.assertEqual(payload["result"]["outcome"], "needs_input")


if __name__ == "__main__":
    unittest.main()
