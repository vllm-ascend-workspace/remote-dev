from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402
import core.shell_ops as shell_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402


class RemoteBashTests(unittest.TestCase):
    def test_remote_bash_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="/tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "cwd_outside_root")

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


if __name__ == "__main__":
    unittest.main()
