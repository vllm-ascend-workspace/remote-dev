from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS  # noqa: E402
from remote_dev.core.ssh_transport import RemoteCompleted  # noqa: E402
import remote_dev.core.shell_ops as shell_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
import remote_dev.core.job_ops as job_ops  # noqa: E402


class RemoteBashTests(unittest.TestCase):
    def test_remote_bash_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="/tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "cwd_outside_root")
        self.assertEqual(
            payload["result"]["next"]["endpoint_patch"],
            {"root": "/tmp", "cwd": "/tmp"},
        )
        self.assertIn("--root /tmp --cwd /tmp", payload["text"])

    def test_remote_bash_core_allows_secret_like_argv(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = shell_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "ok\n", "")

                shell_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = shell_ops.remote_bash(endpoint, command="echo token=abc")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertIn("echo token=abc", scripts[0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            shell_ops.run_script = original_runner  # type: ignore[assignment]

    def test_remote_bash_relative_cwd_hint_does_not_suggest_bad_root(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["next"]["suggested_action"], "rerun_with_absolute_cwd")
        self.assertNotIn("endpoint_patch", payload["result"]["next"])

    def test_remote_bash_success_writes_log_refs(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = shell_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "ok\n", "")

                shell_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = shell_ops.remote_bash(endpoint, command="echo ok")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertTrue(Path(payload["result"]["refs"]["stdout"]).exists())
                self.assertIn('bash -c "$REMOTE_DEV_COMMAND"', scripts[0])
                self.assertNotIn("bash -lc", scripts[0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            shell_ops.run_script = original_runner  # type: ignore[assignment]

    def test_runtime_env_preamble_is_explicit_per_endpoint(self) -> None:
        # No consumer-specific profile script is baked into the substrate: the
        # preamble appears only when the endpoint names a runtime_env_file.
        original_state_root = state_store.substrate_root
        original_runner = shell_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "ok\n", "")

                shell_ops.run_script = fake_run_script  # type: ignore[assignment]
                plain = Endpoint(host="1.2.3.4", port=46000)
                shell_ops.remote_bash(plain, command="echo ok")
                self.assertNotIn("profile.d", scripts[-1])
                self.assertNotIn("set +u; .", scripts[-1])

                configured = Endpoint(host="1.2.3.4", port=46000, runtime_env_file="/etc/profile.d/tool chain.sh")
                payload = shell_ops.remote_bash(configured, command="echo ok")
                self.assertIn("if [ -f '/etc/profile.d/tool chain.sh' ]; then set +u; . '/etc/profile.d/tool chain.sh'; set -u; fi", scripts[-1])
                self.assertEqual(payload["result"]["environment"]["runtime_env_file"], "/etc/profile.d/tool chain.sh")

                shell_ops.remote_bash(configured, command="echo ok", runtime_env=False)
                self.assertNotIn("profile.d", scripts[-1])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            shell_ops.run_script = original_runner  # type: ignore[assignment]

    def test_background_job_records_runtime_env_file_and_restores_it(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, runtime_env_file="/etc/profile.d/toolchain.sh")
        original_state_root = state_store.substrate_root
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_control(_endpoint, job_id, action, **params):
                    calls.append((action, params))
                    return {
                        "state": "prepared" if action == "prepare" else "running",
                        "quiet": False,
                        "gate_open": action == "go",
                        "remote_dir": f"/srv/.remote-dev/jobs/{job_id}",
                    }

                with unittest.mock.patch.object(job_ops, "control", fake_control):
                    payload = job_ops.start_remote_job(endpoint, command="echo ok", job_id="job-runtime-env")
                self.assertEqual(payload["result"]["status"], "running")
                self.assertIn(". /etc/profile.d/toolchain.sh", calls[0][1]["spec"]["command"])
                record = state_store.read_json(Path(payload["result"]["refs"]["job_record"]))
                self.assertEqual(record["runtime_env_file"], "/etc/profile.d/toolchain.sh")
                restored = job_ops.endpoint_from_job_record(record)
                self.assertEqual(restored.runtime_env_file, "/etc/profile.d/toolchain.sh")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]

    def test_background_remote_bash_missing_cwd_does_not_start_job(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app")
        original_state_root = state_store.substrate_root
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_control(_endpoint, _job_id, action, **_params):
                    calls.append(action)
                    raise FileNotFoundError("command cwd does not exist")

                with unittest.mock.patch.object(job_ops, "control", fake_control):
                    payload = shell_ops.remote_bash(
                        endpoint,
                        command="touch /srv/app/should-not-exist",
                        cwd="/srv/app/missing",
                        run_in_background=True,
                    )
                self.assertEqual(payload["result"]["outcome"], "failed")
                self.assertEqual(payload["result"]["status"], "cwd_not_found")
                self.assertEqual(calls, ["prepare"])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]

    def test_background_remote_bash_duplicate_job_id_is_blocked(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                state_store.atomic_write_json(state_store.job_record_path(endpoint, "job-existing"), {"job_id": "job-existing", "target": endpoint.to_result_target()})
                with unittest.mock.patch.object(job_ops, "control", side_effect=AssertionError("control must not run")):
                    payload = job_ops.start_remote_job(endpoint, command="echo ok", job_id="job-existing")
                self.assertEqual(payload["result"]["outcome"], "blocked")
                self.assertEqual(payload["result"]["status"], "job_id_exists")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]

    def test_remote_job_tail_clamps_lines_and_text(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        seen = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                job_id = "job-tail-test"
                state_store.atomic_write_json(
                    state_store.job_record_path(endpoint, job_id),
                    {"job_id": job_id, "target": endpoint.to_result_target(), "remote_dir": "/srv/app/.remote-dev/jobs/job-tail-test"},
                )

                def fake_control(_endpoint, _job_id, action, **params):
                    seen.update(params)
                    return {"state": "running", "quiet": False, "stdout": "x" * (MAX_TEXT_CHARS * 2), "stderr": ""}

                with unittest.mock.patch.object(job_ops, "control", fake_control):
                    payload = job_ops.remote_job_tail(None, job_id=job_id, lines=100000)
                self.assertEqual(seen["lines"], MAX_JOB_TAIL_LINES)
                self.assertIn("clamped", payload["result"]["warnings"][0])
                self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
