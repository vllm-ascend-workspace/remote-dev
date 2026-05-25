from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
import core.file_ops as file_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402


class RemoteEditTests(unittest.TestCase):
    def test_remote_edit_requires_read_ledger(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            original = state_store.substrate_root
            try:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                payload = file_ops.remote_edit(endpoint, file_path="/vllm-workspace/foo.py", old_string="a", new_string="b")
                self.assertEqual(payload["result"]["outcome"], "blocked")
                self.assertEqual(payload["result"]["status"], "read_required")
            finally:
                state_store.substrate_root = original  # type: ignore[assignment]

    def test_remote_edit_with_ledger_returns_changed_file(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                state_store.write_read_ledger(endpoint, {"path": "/vllm-workspace/foo.py", "sha256": "before", "size": 3, "mtime_ns": 1})
                file_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "status": "edited",
                    "file": {"path": "/vllm-workspace/foo.py", "sha256": "after", "size": 3, "mtime_ns": 2},
                    "before_sha256": "before",
                    "after_sha256": "after",
                    "diff_preview": "@@\n-a\n+b\n",
                }
                payload = file_ops.remote_edit(endpoint, file_path="/vllm-workspace/foo.py", old_string="a", new_string="b")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertEqual(payload["result"]["changed_files"][0]["after_sha256"], "after")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
