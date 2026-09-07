from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
import core.ssh_transport as ssh_transport  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
