from __future__ import annotations

import json
import subprocess
import sys
import unittest

from remote_dev.cli import TOOL_NAMES
from remote_dev.mcp.schemas import TOOL_SCHEMAS


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "remote_dev", *args], capture_output=True, text=True, check=False)


class CliHelpTests(unittest.TestCase):
    def test_cli_wrappers_have_help(self) -> None:
        expected = {name.removeprefix("remote.") for name in TOOL_SCHEMAS}
        self.assertEqual(set(TOOL_NAMES), expected)
        proc = _cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)
        self.assertIn("server", proc.stdout)
        self.assertIn("status", proc.stdout)
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                help_proc = _cli(name.replace("_", "-"), "--help")
                self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
                self.assertIn("usage:", help_proc.stdout)

    def test_cli_endpoint_flags_are_explicit_only(self) -> None:
        proc = _cli("bash", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--host", "--port", "--user", "--root", "--cwd", "--alias", "--selector", "--runtime-env-file"):
            self.assertIn(flag, proc.stdout)
        for legacy in ("--session-id", "--session-file", "--machine"):
            self.assertNotIn(legacy, proc.stdout)

    def test_cli_selector_without_resolver_is_endpoint_required(self) -> None:
        proc = _cli("probe", "--selector", "session_id=abc")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["tool"], "remote.probe")
        self.assertEqual(payload["result"]["status"], "endpoint_required")
        self.assertIn("registered resolvers", payload["result"]["error"])

    def test_cli_bad_selector_item_is_invalid_input(self) -> None:
        proc = _cli("probe", "--selector", "novalue")
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["status"], "invalid_input")

    def test_package_import_does_not_mutate_sys_path(self) -> None:
        code = (
            "import sys\n"
            "before = list(sys.path)\n"
            "import remote_dev.mcp.tools, remote_dev.mcp.server, remote_dev.core.endpoint\n"
            "print([p for p in sys.path if p not in before])\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_cli_errors_return_result_contract_without_traceback(self) -> None:
        proc = _cli("job-status", "--job-id", "job-does-not-exist")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "remote.job_status")
        self.assertEqual(payload["result"]["outcome"], "needs_input")


if __name__ == "__main__":
    unittest.main()
