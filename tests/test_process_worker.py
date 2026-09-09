"""Linux process-level checks for the shipped remote worker.

No NPU, SSH, or coordinator. Skipped on macOS/Windows: the remote worker
target is Linux /proc + prctl child-subreaper.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from remote_dev.processes import worker_source


def load_worker():
    path = Path(__file__).resolve().parents[1] / "remote_dev" / "processes" / "worker.py"
    spec = importlib.util.spec_from_file_location("remote_dev_process_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc identity and prctl subreaper")
class ProcessWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_worker()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = worker_source()
        self.identifiers = []

    def tearDown(self):
        for identifier in self.identifiers:
            for _ in range(50):
                status = self.call(identifier, "stop", force=True)
                if status.get("quiet"):
                    break
                time.sleep(0.02)
        self.temp.cleanup()

    def call(self, identifier, action, **args):
        return self.worker.control_job(
            {"root": str(self.root), "job_id": identifier, "action": action, **args},
            self.source,
        )

    def prepare(self, letter, command, timeout=10):
        identifier = "job-" + letter * 8
        self.identifiers.append(identifier)
        status = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": command, "env": {}, "timeout_seconds": timeout})
        return identifier, status

    def go(self, identifier):
        return self.call(identifier, "go", authorization={"token": identifier, "kind": "test"})

    def until(self, identifier, condition):
        status = None
        for _ in range(100):
            status = self.call(identifier, "status")
            if condition(status):
                return status
            time.sleep(0.05)
        self.fail("process worker did not reach the expected bounded state: " + json.dumps(status))

    def test_waiting_gate_is_idempotent_and_command_does_not_run_early(self):
        identifier, first = self.prepare("a", "printf completed > result.txt")
        second = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": "printf completed > result.txt", "env": {}, "timeout_seconds": 10})
        self.assertEqual(first["receipt"]["pid"], second["receipt"]["pid"])
        self.assertEqual(second["state"], "prepared")
        self.assertFalse((self.root / "result.txt").exists())
        self.go(identifier)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "succeeded")
        self.assertEqual((self.root / "result.txt").read_text(), "completed")

    def test_stop_clean_environment_daemon_keeps_the_other_family_alive(self):
        command = (
            "setsid env -u REMOTE_DEV_JOB_TOKEN "
            + shlex.quote(sys.executable)
            + " -c "
            + shlex.quote("import os,time; from pathlib import Path; Path('daemon.pid').write_text(str(os.getpid())); time.sleep(60)")
            + " &"
        )
        a, _ = self.prepare("a", command, timeout=60)
        b, before = self.prepare("b", "sleep 60 & wait", timeout=60)
        self.go(a)
        self.go(b)
        self.until(a, lambda row: (self.root / "daemon.pid").exists())
        daemon = int((self.root / "daemon.pid").read_text())
        observed = self.call(a, "status")
        self.assertIn(daemon, [row["pid"] for row in observed["processes"]])
        self.assertNotIn(b"REMOTE_DEV_JOB_TOKEN=", Path(f"/proc/{daemon}/environ").read_bytes())
        self.assertNotEqual(self.worker.process_identity(daemon)["pgid"], observed["receipt"]["pgid"])
        self.call(a, "stop", force=True)
        self.until(a, lambda row: row["quiet"])
        self.assertIsNone(self.worker.process_identity(daemon))
        after = self.call(b, "status")
        self.assertFalse(after["quiet"])
        self.assertEqual(before["receipt"]["pid"], after["receipt"]["pid"])

    def test_stop_before_go_never_executes_and_unknown_receipt_is_not_free(self):
        identifier, _ = self.prepare("a", "touch should-not-exist")
        self.call(identifier, "stop", force=True)
        self.until(identifier, lambda row: row["quiet"])
        with self.assertRaises(RuntimeError):
            self.go(identifier)
        self.assertFalse((self.root / "should-not-exist").exists())
        directory = self.root / ".remote-dev/jobs" / "job-bbbbbbbb"
        directory.mkdir(parents=True)
        (directory / "intent.json").write_text("{}")
        status = self.call("job-bbbbbbbb", "status")
        self.assertEqual(status["state"], "uncertain")
        self.assertFalse(status["quiet"])

    def test_timeout_is_not_reported_as_a_success_or_manual_cancel(self):
        identifier, _ = self.prepare("a", "sleep 60", timeout=1)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "timeout")

    def test_background_descendant_cannot_outlive_the_bounded_execution_unobserved(self):
        identifier, _ = self.prepare("a", "sleep 60 &", timeout=1)
        self.go(identifier)
        observed = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(observed["state"], "timeout")
        self.assertTrue(observed["result"]["descendants_drained"])

    def test_lost_supervisor_cannot_report_quiet(self):
        identifier, first = self.prepare("a", "touch should-not-run")
        os.kill(first["receipt"]["pid"], signal.SIGKILL)
        observed = self.until(identifier, lambda row: not row.get("processes"))
        self.assertEqual(observed["state"], "uncertain")
        self.assertFalse(observed["quiet"])
        self.assertTrue(first["receipt"]["process_guard"].get("retain_until_release"))
        self.assertFalse((self.root / "should-not-run").exists())

    def test_legacy_receipt_stop_without_result_reports_cancelled(self):
        identifier = "job-cccccccc"
        directory = self.root / ".remote-dev/jobs" / identifier
        directory.mkdir(parents=True)
        pid = 2**22
        while Path(f"/proc/{pid}").exists():
            pid -= 1
        (directory / "receipt.json").write_text(json.dumps({
            "pid": pid, "ppid": 1, "pgid": pid, "start_ticks": "0",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "marker": "d" * 32, "job_id": identifier, "prepared_at": time.time()}))
        (directory / "stop.json").write_text("{}")
        observed = self.call(identifier, "status")
        self.assertEqual(observed["state"], "cancelled")
        self.assertTrue(observed["quiet"])

    def test_jobs_live_under_remote_dev_not_vaws_runtime(self):
        identifier, status = self.prepare("z", "true")
        remote_dir = Path(status["remote_dir"])
        self.assertEqual(remote_dir, self.root / ".remote-dev" / "jobs" / identifier)
        self.assertFalse((self.root / ".vaws-runtime").exists())


@unittest.skipIf(sys.platform == "win32", "Linux worker is not a native Windows module")
class ProcessWorkerEntrypointTests(unittest.TestCase):
    def test_main_without_control_bootstrap_fails_with_clear_message(self):
        script = Path(__file__).resolve().parents[1] / "remote_dev" / "processes" / "worker.py"
        request = {"root": "/tmp", "job_id": "job-eeeeeeee", "action": "status"}
        completed = subprocess.run(
            [sys.executable, str(script), json.dumps(request)],
            capture_output=True, text=True, check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WORKER_SOURCE is undefined", completed.stderr)
        self.assertIn("remote_dev.processes.control", completed.stderr)


if __name__ == "__main__":
    unittest.main()
