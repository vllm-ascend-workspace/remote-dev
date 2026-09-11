from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from remote_dev.cli import TOOL_NAMES
from remote_dev.mcp.schemas import TOOL_SCHEMAS


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "remote_dev", *args], capture_output=True, text=True, check=False)


class CliHelpTests(unittest.TestCase):
    def test_parser_feedback_does_not_import_transport_or_start_processes(self) -> None:
        guard = '''
import importlib.abc, sys
class NoBackend(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("remote_dev.core.") and (fullname.endswith("_ops") or fullname.endswith("ssh_transport")):
            raise AssertionError("parser loaded backend: " + fullname)
sys.meta_path.insert(0, NoBackend())
def audit(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system"}:
        raise AssertionError("parser attempted side effect: " + event)
sys.addaudithook(audit)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sitecustomize.py").write_text(guard, encoding="utf-8")
            home = root / "home"
            home.mkdir()
            env = os.environ.copy()
            source = str(Path(__file__).resolve().parents[1])
            env.update(PYTHONPATH=os.pathsep.join((str(root), source)), HOME=str(home), USERPROFILE=str(home))
            for key in list(env):
                if key.startswith("REMOTE_DEV_"):
                    env.pop(key)
            cases = [(["--help"], 0), (["--unknown-option"], 2), (["read"], 1)]
            cases.extend(([name.replace("_", "-"), "--help"], 0) for name in TOOL_NAMES)
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    result = subprocess.run(
                        [sys.executable, "-m", "remote_dev", *argv], env=env,
                        capture_output=True, encoding="utf-8", timeout=15,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertNotIn("AssertionError", result.stdout + result.stderr)
                    if argv == ["read"]:
                        self.assertEqual(json.loads(result.stdout)["result"]["status"], "endpoint_required")
            self.assertEqual(list(home.iterdir()), [])

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

    def test_cli_consumes_local_input_files_before_tool_argument_validation(self) -> None:
        from unittest import mock
        from remote_dev import cli
        from remote_dev.mcp import tools
        with tempfile.TemporaryDirectory() as directory:
            content = Path(directory) / "content.txt"
            content.write_text("file body", encoding="utf-8")
            arguments = Path(directory) / "arguments.json"
            arguments.write_text(json.dumps({"file_path": "/tmp/example", "overwrite": True}), encoding="utf-8")
            args = cli.build_parser("write").parse_args(["--host", "example.invalid", "--port", "22",
                "--content-file", str(content), "--input-json", str(arguments)])
            with mock.patch.object(tools, "remote_write", return_value={"ok": True}) as write:
                self.assertEqual(cli.run_tool("write", args), {"ok": True})
                self.assertEqual(write.call_args.kwargs["content"], "file body")
                self.assertEqual(write.call_args.kwargs["file_path"], "/tmp/example")
                self.assertTrue(write.call_args.kwargs["overwrite"])

    def test_cli_payload_maps_ssh_mux_keepalive_and_long_stream_flags(self) -> None:
        from remote_dev.cli import build_parser, endpoint_payload

        parser = build_parser("probe")
        args = parser.parse_args(["--host", "192.0.2.10", "--port", "22", "--no-ssh-mux", "--keepalive"])
        payload = endpoint_payload(args)
        self.assertIs(payload["ssh_mux"], False)
        self.assertIs(payload["keepalive"], True)
        stream_args = parser.parse_args(["--host", "192.0.2.10", "--port", "22", "--long-stream"])
        stream_payload = endpoint_payload(stream_args)
        self.assertIs(stream_payload["ssh_mux"], False)
        self.assertIs(stream_payload["keepalive"], True)
        default_args = parser.parse_args(["--host", "192.0.2.10", "--port", "22"])
        default_payload = endpoint_payload(default_args)
        self.assertNotIn("ssh_mux", default_payload)
        self.assertNotIn("keepalive", default_payload)
        with self.assertRaises(ValueError) as raised:
            endpoint_payload(parser.parse_args(["--host", "192.0.2.10", "--port", "22", "--ssh-mux", "--long-stream"]))
        self.assertIn("rc=0", str(raised.exception))
        self.assertIn("first-option-wins", str(raised.exception))

    def test_cli_endpoint_flags_are_explicit_only(self) -> None:
        proc = _cli("bash", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in (
            "--host",
            "--port",
            "--user",
            "--root",
            "--cwd",
            "--alias",
            "--selector",
            "--runtime-env-file",
            "--ssh-mux",
            "--no-ssh-mux",
            "--keepalive",
            "--long-stream",
        ):
            self.assertIn(flag, proc.stdout)
        for legacy in ("--session-id", "--session-file", "--machine"):
            self.assertNotIn(legacy, proc.stdout)

    def test_cli_selector_without_resolver_is_endpoint_required(self) -> None:
        proc = _cli("probe", "--selector", "lab=gpu-1")
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
