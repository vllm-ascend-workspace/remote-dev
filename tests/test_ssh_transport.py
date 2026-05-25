from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
