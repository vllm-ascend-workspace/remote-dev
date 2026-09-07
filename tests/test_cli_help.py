from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.schemas import TOOL_SCHEMAS  # noqa: E402


class CliHelpTests(unittest.TestCase):
    def test_cli_wrappers_have_help(self) -> None:
        scripts = sorted((ROOT / "tools").glob("remote_*.py"))
        expected_scripts = {ROOT / "tools" / (name.replace(".", "_") + ".py") for name in TOOL_SCHEMAS}
        self.assertEqual(set(scripts), expected_scripts)
        for script_path in scripts:
            script = str(script_path.relative_to(ROOT))
            with self.subTest(script=script):
                proc = subprocess.run([sys.executable, str(script_path), "--help"], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_cli_endpoint_flags_are_explicit_only(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "remote_bash.py"), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--host", "--port", "--user", "--root", "--cwd", "--alias", "--selector", "--runtime-env-file"):
            self.assertIn(flag, proc.stdout)
        # Consumer-specific selectors are no longer first-class CLI flags; they
        # travel through --selector KEY=VALUE to a registered resolver.
        for legacy in ("--session-id", "--session-file", "--machine"):
            self.assertNotIn(legacy, proc.stdout)

    def test_cli_selector_without_resolver_is_endpoint_required(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "remote_probe.py"), "--selector", "session_id=abc"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["tool"], "remote.probe")
        self.assertEqual(payload["result"]["status"], "endpoint_required")
        self.assertIn("registered resolvers", payload["result"]["error"])

    def test_cli_bad_selector_item_is_invalid_input(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "remote_probe.py"), "--selector", "novalue"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["status"], "invalid_input")

    def test_core_imports_do_not_reach_outside_the_checkout(self) -> None:
        # A standalone deployment has no consumer beside it. Importing every
        # tool entry point must not add any path outside this checkout to
        # sys.path or import a consumer package by name.
        code = (
            "import sys; sys.path.insert(0, %r); import mcp.tools, mcp.server, core.endpoint\n"
            "root = %r\n"
            "leaks = [p for p in sys.path if p and not p.startswith(root) and p not in sys_path_before]\n"
            "print(leaks)"
        )
        preamble = "import sys; sys_path_before = list(sys.path)\n"
        proc = subprocess.run([sys.executable, "-c", preamble + code % (str(ROOT), str(ROOT))], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_cli_errors_return_result_contract_without_traceback(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "remote_job_status.py"),
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
