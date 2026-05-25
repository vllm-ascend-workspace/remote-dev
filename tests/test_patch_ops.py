from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.patch_ops import parse_codex_patch, parse_unified_patch_paths  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
import core.patch_ops as patch_ops  # noqa: E402


class PatchOpsTests(unittest.TestCase):
    def test_parse_codex_update_patch(self) -> None:
        patch = """*** Begin Patch
*** Update File: foo.py
@@
 old line
-remove
+add
 keep
*** End Patch
"""
        ops = parse_codex_patch(patch)
        self.assertEqual(ops[0]["kind"], "update")
        self.assertEqual(ops[0]["path"], "foo.py")
        self.assertIn("remove\n", ops[0]["hunks"][0]["old"])
        self.assertIn("add\n", ops[0]["hunks"][0]["new"])

    def test_parse_codex_add_patch(self) -> None:
        patch = """*** Begin Patch
*** Add File: new.py
+print("hi")
*** End Patch
"""
        ops = parse_codex_patch(patch)
        self.assertEqual(ops, [{"kind": "add", "path": "new.py", "content": 'print("hi")\n'}])

    def test_parse_codex_move_patch(self) -> None:
        patch = """*** Begin Patch
*** Update File: old.py
*** Move to: new.py
*** End Patch
"""
        ops = parse_codex_patch(patch)
        self.assertEqual(ops, [{"kind": "update", "path": "old.py", "hunks": [], "move_to": "new.py"}])

    def test_parse_codex_end_of_file_marker(self) -> None:
        patch = """*** Begin Patch
*** Update File: foo.py
@@
-old
+new
*** End of File
*** End Patch
"""
        ops = parse_codex_patch(patch)
        self.assertEqual(ops[0]["hunks"], [{"old": "old\n", "new": "new\n"}])

    def test_parse_unified_paths(self) -> None:
        patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-a
+b
"""
        self.assertEqual(parse_unified_patch_paths(patch), ["a.py"])

    def test_remote_apply_patch_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        patch = """*** Begin Patch
*** Add File: /tmp/outside.py
+x = 1
*** End Patch
"""
        payload = patch_ops.remote_apply_patch(endpoint, patch=patch)
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_remote_apply_patch_move_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        patch = """*** Begin Patch
*** Update File: foo.py
*** Move to: /tmp/outside.py
*** End Patch
"""
        payload = patch_ops.remote_apply_patch(endpoint, patch=patch)
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_codex_patch_executor_moves_and_edits_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old.py"
            target = root / "new.py"
            source.write_text("old\n", encoding="utf-8")
            payload = {
                "root": str(root),
                "cwd": str(root),
                "ops": [{
                    "kind": "update",
                    "path": "old.py",
                    "move_to": "new.py",
                    "hunks": [{"old": "old\n", "new": "new\n"}],
                }],
            }
            proc = subprocess.run(
                [sys.executable, "-c", patch_ops.REMOTE_CODEX_PATCH_PY],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "applied")
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_unified_patch_records_before_sha_and_diffstat(self) -> None:
        original_runner = patch_ops.run_script
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                (repo / ".git").mkdir()
                target = repo / "a.py"
                target.write_text("old\n", encoding="utf-8")
                expected_before = hashlib.sha256(target.read_bytes()).hexdigest()
                script_endpoint = Endpoint(host="1.2.3.4", port=46000, root=str(repo), cwd=str(repo))
                from core.ssh_transport import RemoteCompleted

                def fake_run_script(_endpoint, script, **_kwargs):
                    proc = subprocess.run(["bash", "-s"], input=script, cwd=repo, capture_output=True, text=True, check=False)
                    return RemoteCompleted(proc.returncode, proc.stdout, proc.stderr)

                patch_ops.run_script = fake_run_script  # type: ignore[assignment]
                patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old
+new
"""
                payload = patch_ops.remote_apply_patch(script_endpoint, patch=patch, cwd=str(repo))
                self.assertEqual(payload["result"]["outcome"], "success", payload["text"])
                changed = payload["result"]["changed_files"][0]
                self.assertEqual(changed["before_sha256"], expected_before)
                self.assertIsNotNone(changed["after_sha256"])
                self.assertIn("a.py", payload["result"]["preview"]["diffstat"])
        finally:
            patch_ops.run_script = original_runner  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
