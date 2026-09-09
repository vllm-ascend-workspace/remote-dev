"""Client-side process control contract. No SSH, no coordinator install."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.processes import control, worker_source
import remote_dev.processes.client as control_mod
import remote_dev.core.ssh_transport as ssh_transport


class ProcessControlClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, root="/srv/app", cwd="/srv/app")

    def test_worker_source_is_packaged_and_generic(self) -> None:
        source = worker_source()
        self.assertIn("PR_SET_CHILD_SUBREAPER", source)
        self.assertIn("REMOTE_DEV_JOB_TOKEN", source)
        self.assertIn("control_job", source)
        self.assertNotIn("VAWS_REMOTE_JOB", source)
        self.assertNotIn(".vaws-runtime", source)
        self.assertNotIn("lease/fence", source)
        self.assertNotIn("NPU", source)

    def test_control_is_exported_at_the_package_boundary(self) -> None:
        import remote_dev.processes as processes

        self.assertTrue(callable(processes.control))
        self.assertIs(processes.control, control)
        self.assertFalse(hasattr(processes.control, "control"))

    def test_control_ships_worker_as_json_stdin_without_a_local_shell(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["input"] = kwargs.get("input")
            observed["kwargs"] = kwargs
            result = {"ok": True, "result": {"state": "prepared", "quiet": False, "gate_open": False}}
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(result), stderr="")

        with mock.patch.object(ssh_transport.subprocess, "run", fake_run):
            payload = control(
                self.endpoint,
                "job-abc123",
                "prepare",
                spec={"command": "true", "cwd": "/srv/app", "env": {}, "timeout_seconds": 10},
            )

        self.assertEqual(payload["state"], "prepared")
        args = observed["args"]
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], "ssh")
        self.assertTrue(str(args[-1]).startswith("python3 -c "), args[-1])
        self.assertNotIn("bash", args)
        self.assertNotIn("<<", " ".join(str(item) for item in args))
        self.assertIsNone(observed["kwargs"].get("shell"))
        body = json.loads(str(observed["input"]))
        self.assertEqual(body["request"]["job_id"], "job-abc123")
        self.assertEqual(body["request"]["action"], "prepare")
        self.assertEqual(body["request"]["root"], "/srv/app")
        self.assertIn("PR_SET_CHILD_SUBREAPER", body["worker_source"])
        self.assertNotIn("VAWS_REMOTE_JOB", body["worker_source"])

    def test_control_accepts_an_ordinary_host_port_mapping(self) -> None:
        def fake_run_remote_python(_endpoint, _code, payload, **_kwargs):
            self.assertEqual(payload["request"]["root"], "/work")
            self.assertEqual(payload["request"]["action"], "status")
            return {"ok": True, "result": {"state": "absent", "quiet": True}}

        with mock.patch.object(control_mod, "run_remote_python", fake_run_remote_python):
            row = control({"host": "192.0.2.8", "port": 22, "root": "/work"}, "job-map", "status")
        self.assertEqual(row, {"state": "absent", "quiet": True})

    def test_control_reraises_remote_value_errors(self) -> None:
        with mock.patch.object(
            control_mod,
            "run_remote_python",
            return_value={"ok": False, "type": "ValueError", "error": "invalid job id"},
        ):
            with self.assertRaises(ValueError) as raised:
                control(self.endpoint, "??", "status")
        self.assertIn("invalid job id", str(raised.exception))

    def test_control_rejects_unknown_actions_locally(self) -> None:
        with self.assertRaises(ValueError):
            control(self.endpoint, "job-abc123", "lease")

    def test_control_transport_failure_is_remote_execution_error(self) -> None:
        with mock.patch.object(
            control_mod,
            "run_remote_python",
            return_value={"status": "failed", "error": "remote python failed", "stderr_tail": "ssh: connect failed"},
        ):
            with self.assertRaises(RemoteExecutionError) as raised:
                control(self.endpoint, "job-abc123", "status")
        self.assertIn("ssh: connect failed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
