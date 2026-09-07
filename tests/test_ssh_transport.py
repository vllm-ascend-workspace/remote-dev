from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.errors import RemoteExecutionError  # noqa: E402
import core.ssh_transport as ssh_transport  # noqa: E402

SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"

_CHILD_SSH_ARGV = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from core.endpoint import Endpoint
import core.ssh_transport as t
t._MUX_READY = True
endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")
print(json.dumps(t.ssh_base_cmd(endpoint)))
"""


def _option_map(cmd: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(cmd):
        if cmd[index] == "-o" and index + 1 < len(cmd):
            key, _, value = cmd[index + 1].partition("=")
            values[key] = value
            index += 2
            continue
        index += 1
    return values


class SshTransportTests(unittest.TestCase):
    def test_run_remote_python_quotes_multiline_code_as_one_remote_command(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"status":"ok"}', stderr="")

        with mock.patch.object(ssh_transport.subprocess, "run", fake_run):
            payload = ssh_transport.run_remote_python(endpoint, "import json\nprint(json.dumps({'status':'ok'}))", {})

        args = observed["args"]
        self.assertIsInstance(args, list)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(args[-1].split(" ", 2)[:2], ["python3", "-c"])
        self.assertIn("\\n", repr(args[-1]))

    def test_run_bytes_quotes_shell_command_as_one_remote_command(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(ssh_transport.subprocess, "run", fake_run):
            ssh_transport.run_bytes(endpoint, "cat '/tmp/path with spaces'")

        args = observed["args"]
        self.assertIsInstance(args, list)
        self.assertEqual(args[-1].split(" ", 2)[:2], ["bash", "-c"])
        self.assertIn("path with spaces", args[-1])

    @staticmethod
    def _control_path(options: list[str]) -> str:
        return next(option for option in options if option.startswith("ControlPath="))

    def test_control_path_is_scoped_per_identity_file(self) -> None:
        # OpenSSH %C does not hash the identity file, so endpoints that differ
        # only by SSH key must get distinct ControlPath sockets (D9).
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            plain = self._control_path(ssh_transport._control_master_options())
            first = self._control_path(ssh_transport._control_master_options("/keys/a"))
            second = self._control_path(ssh_transport._control_master_options("/keys/b"))
        self.assertTrue(plain.endswith("/%C"))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, plain)
        self.assertTrue(first.startswith(plain))

    def test_mux_dir_defaults_under_home_and_honours_env_override(self) -> None:
        # The mux directory is remote-dev's own by default; a consumer that
        # wants to share its existing OpenSSH mux dir sets REMOTE_DEV_SSH_MUX_DIR.
        self.assertEqual(ssh_transport._MUX_DIR, Path.home() / ".ssh" / "remote-dev-mux")
        code = (
            "import sys; sys.path.insert(0, %r); import core.ssh_transport as t; print(t._MUX_DIR)" % str(ROOT)
        )
        env = {**os.environ, "REMOTE_DEV_SSH_MUX_DIR": "/tmp/shared-mux"}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "/tmp/shared-mux")

    def test_ssh_base_cmd_passes_identity_file_into_control_path(self) -> None:
        with_identity = Endpoint(host="1.2.3.4", port=46000, identity_file="/keys/a")
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            options = ssh_transport._control_master_options(with_identity.identity_file)
            expected = self._control_path(options)
            cmd = ssh_transport.ssh_base_cmd(with_identity)
        self.assertIn(expected, cmd)
        self.assertIn("/keys/a", cmd)

    def test_ssh_base_cmd_cannot_turn_user_or_host_into_an_option(self) -> None:
        # A user or host that begins with `-` must stay an argument of `-l`
        # / the destination after `--`. OpenSSH would otherwise treat
        # `-oProxyCommand=...` as an option and run a local command.
        endpoint = Endpoint(host="-oProxyCommand=marker", port=22, user="-oProxyCommand=evil")
        with mock.patch.object(ssh_transport, "_MUX_READY", False):
            cmd = ssh_transport.ssh_base_cmd(endpoint)
        self.assertEqual(cmd[cmd.index("-l") + 1], "-oProxyCommand=evil")
        self.assertEqual(cmd[cmd.index("--") + 1], "-oProxyCommand=marker")
        self.assertLess(cmd.index("-l"), cmd.index("--"))
        self.assertNotIn("-oProxyCommand=evil@-oProxyCommand=marker", cmd)


class SshMuxIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")

    def _cmd_with_mux(self, value: str | None, *, mux_ready: bool = True) -> list[str]:
        with mock.patch.object(ssh_transport, "_MUX_READY", mux_ready):
            with mock.patch.dict(os.environ):
                if value is None:
                    os.environ.pop(SSH_MUX_ENV, None)
                else:
                    os.environ[SSH_MUX_ENV] = value
                return ssh_transport.ssh_base_cmd(self.endpoint)

    def test_mux_zero_emits_independent_connection_options(self) -> None:
        cmd = self._cmd_with_mux("0")
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertNotIn("ControlMaster=auto", cmd)
        self.assertFalse(any(item.startswith("ControlPath=") and item != "ControlPath=none" for item in cmd))

    def test_unset_and_explicit_one_keep_shared_mux_and_identity_suffix(self) -> None:
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            expected_path = next(
                item for item in ssh_transport._control_master_options("/keys/a") if item.startswith("ControlPath=")
            )
        for value in (None, "1"):
            with self.subTest(REMOTE_DEV_SSH_MUX=value):
                cmd = self._cmd_with_mux(value)
                options = _option_map(cmd)
                self.assertEqual(options["ControlMaster"], "auto")
                self.assertEqual(options["ControlPersist"], "120")
                self.assertEqual("ControlPath=" + options["ControlPath"], expected_path)
                self.assertIn("%C-", options["ControlPath"])
                self.assertIn(expected_path, cmd)

    def test_mux_zero_bypasses_helper_and_does_not_touch_mux_state(self) -> None:
        real_helper = ssh_transport._control_master_options
        for ready in (None, True, False):
            with self.subTest(mux_ready=ready):
                with mock.patch.object(ssh_transport, "_MUX_READY", ready):
                    with mock.patch.object(Path, "mkdir") as mkdir:
                        with mock.patch.object(Path, "rmdir") as rmdir:
                            with mock.patch.object(Path, "unlink") as unlink:
                                with mock.patch.object(Path, "chmod") as path_chmod:
                                    with mock.patch.object(ssh_transport.os, "chmod") as chmod:
                                        with mock.patch.object(
                                            ssh_transport,
                                            "_control_master_options",
                                            wraps=real_helper,
                                        ) as helper:
                                            with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
                                                cmd = ssh_transport.ssh_base_cmd(self.endpoint)
                                        self.assertEqual(ssh_transport._MUX_READY, ready)
                                        helper.assert_not_called()
                                        mkdir.assert_not_called()
                                        rmdir.assert_not_called()
                                        unlink.assert_not_called()
                                        path_chmod.assert_not_called()
                                        chmod.assert_not_called()
                                        options = _option_map(cmd)
                                        self.assertEqual(options["ControlMaster"], "no")
                                        self.assertEqual(options["ControlPath"], "none")
                                        self.assertEqual(options["ControlPersist"], "no")

    def test_runners_honor_mux_zero_and_keep_quoting_and_results(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            if kwargs.get("text"):
                return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"status":"ok"}', stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"bytes-out", stderr=b"")

        with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
            with mock.patch.object(ssh_transport.subprocess, "run", fake_run):
                script_result = ssh_transport.run_script(self.endpoint, "echo hi")
                script_args = list(observed["args"])
                bytes_result = ssh_transport.run_bytes(self.endpoint, "cat '/tmp/path with spaces'", stdin=b"abc")
                bytes_args = list(observed["args"])
                payload = ssh_transport.run_remote_python(
                    self.endpoint,
                    "import json\nprint(json.dumps({'status':'ok'}))",
                    {},
                )
                python_args = list(observed["args"])

        for args in (script_args, bytes_args, python_args):
            options = _option_map(args)
            self.assertEqual(options["ControlMaster"], "no")
            self.assertEqual(options["ControlPath"], "none")
            self.assertEqual(options["ControlPersist"], "no")
            self.assertEqual(args[0], "ssh")
            self.assertEqual(args[args.index("-l") + 1], self.endpoint.user)
            self.assertEqual(args[args.index("--") + 1], self.endpoint.host)

        self.assertEqual(script_args[-2:], ["bash", "-s"])
        self.assertEqual(script_result.returncode, 0)
        self.assertEqual(script_result.stdout, '{"status":"ok"}')
        self.assertFalse(script_result.timed_out)

        self.assertEqual(bytes_args[-1].split(" ", 2)[:2], ["bash", "-c"])
        self.assertIn("path with spaces", bytes_args[-1])
        self.assertEqual(bytes_result.returncode, 0)
        self.assertEqual(bytes_result.stdout, b"bytes-out")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(python_args[-1].split(" ", 2)[:2], ["python3", "-c"])
        self.assertIn("\\n", repr(python_args[-1]))

    def test_subprocess_copies_keep_independent_argv_and_parent_env(self) -> None:
        parent_before = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.environ.copy()
            shared["REMOTE_DEV_SSH_MUX_DIR"] = tmp
            zero_env = dict(shared)
            zero_env[SSH_MUX_ENV] = "0"
            one_env = dict(shared)
            one_env[SSH_MUX_ENV] = "1"
            unset_env = dict(shared)
            unset_env.pop(SSH_MUX_ENV, None)

            zero = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV, str(ROOT)],
                capture_output=True,
                text=True,
                check=False,
                env=zero_env,
            )
            one = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV, str(ROOT)],
                capture_output=True,
                text=True,
                check=False,
                env=one_env,
            )
            unset = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV, str(ROOT)],
                capture_output=True,
                text=True,
                check=False,
                env=unset_env,
            )

        self.assertEqual(dict(os.environ), parent_before)
        self.assertEqual(zero.returncode, 0, zero.stderr)
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertEqual(unset.returncode, 0, unset.stderr)
        zero_cmd = json.loads(zero.stdout)
        one_cmd = json.loads(one.stdout)
        unset_cmd = json.loads(unset.stdout)
        zero_options = _option_map(zero_cmd)
        one_options = _option_map(one_cmd)
        unset_options = _option_map(unset_cmd)
        self.assertEqual(zero_options["ControlMaster"], "no")
        self.assertEqual(zero_options["ControlPath"], "none")
        self.assertEqual(zero_options["ControlPersist"], "no")
        self.assertEqual(one_options["ControlMaster"], "auto")
        self.assertEqual(one_options["ControlPersist"], "120")
        self.assertIn("%C-", one_options["ControlPath"])
        self.assertEqual(unset_options, one_options)
        self.assertNotEqual(zero_options["ControlMaster"], one_options["ControlMaster"])
        self.assertEqual(parent_before.get(SSH_MUX_ENV), os.environ.get(SSH_MUX_ENV))

    def test_openssh_G_evaluates_independent_mux_options(self) -> None:
        if shutil.which("ssh") is None:
            self.skipTest("OpenSSH ssh is not available for -G configuration evaluation")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "ssh_config"
            fake_path = str(Path(tmp) / "cm-%C")
            config_path.write_text(
                "Host *\n"
                "  ControlMaster auto\n"
                f"  ControlPath {fake_path}\n"
                "  ControlPersist 120\n"
            )
            cmd = self._cmd_with_mux("0")
            eval_cmd = [cmd[0], "-G", "-F", str(config_path), *cmd[1:]]
            proc = subprocess.run(eval_cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                self.skipTest("ssh -G configuration evaluation is unavailable" + (f": {detail}" if detail else ""))
            parsed: dict[str, str] = {}
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                key, _, value = line.partition(" ")
                parsed[key.lower()] = value.strip()
            if "controlmaster" not in parsed or "controlpersist" not in parsed:
                keys = ",".join(sorted(parsed)[:30])
                sample = (proc.stdout or proc.stderr or "")[:200]
                self.skipTest(
                    "ssh -G did not report ControlMaster/ControlPersist "
                    f"(keys={keys!r} sample={sample!r})"
                )
            path = parsed.get("controlpath", "none")
            self.assertIn(parsed["controlmaster"].lower(), {"false", "no"})
            self.assertEqual(path.lower(), "none")
            self.assertIn(parsed["controlpersist"].lower(), {"no", "0", "false"})
            self.assertNotIn(fake_path.lower(), path.lower())
            self.assertNotIn(fake_path, proc.stdout)

    def test_unsupported_mux_values_are_configuration_errors(self) -> None:
        for value in ("", "2", "false", "true", "off", "yes", "no", "auto", "00"):
            with self.subTest(REMOTE_DEV_SSH_MUX=value):
                with self.assertRaises(RemoteExecutionError) as raised:
                    self._cmd_with_mux(value)
                message = str(raised.exception)
                self.assertIn(SSH_MUX_ENV, message)
                self.assertIn("0", message)
                self.assertIn("1", message)

    def test_mux_zero_preserves_identity_timeout_and_option_injection(self) -> None:
        dashed = Endpoint(
            host="-oProxyCommand=marker",
            port=22,
            user="-oProxyCommand=evil",
            identity_file="/keys/a",
            connect_timeout_ms=5000,
        )
        with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
            cmd = ssh_transport.ssh_base_cmd(dashed)
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["ConnectTimeout"], "5")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/keys/a")
        self.assertEqual(cmd[cmd.index("-l") + 1], "-oProxyCommand=evil")
        self.assertEqual(cmd[cmd.index("--") + 1], "-oProxyCommand=marker")
        self.assertLess(cmd.index("-l"), cmd.index("--"))
        self.assertNotIn("-oProxyCommand=evil@-oProxyCommand=marker", cmd)

    def test_mux_override_does_not_write_environ(self) -> None:
        before = os.environ.copy()
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
                ssh_transport.ssh_base_cmd(self.endpoint)
                self.assertEqual(os.environ.get(SSH_MUX_ENV), "0")
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                ssh_transport.ssh_base_cmd(self.endpoint)
                self.assertNotIn(SSH_MUX_ENV, os.environ)
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
