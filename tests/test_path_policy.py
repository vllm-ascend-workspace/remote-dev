from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.errors import PathPolicyError  # noqa: E402
from core.path_policy import assert_under_root, join_under_root  # noqa: E402


class PathPolicyTests(unittest.TestCase):
    def test_join_under_root_accepts_relative_child(self) -> None:
        self.assertEqual(join_under_root("/vllm-workspace", "/vllm-workspace/src", "foo.py"), "/vllm-workspace/src/foo.py")

    def test_join_under_root_rejects_escape(self) -> None:
        with self.assertRaises(PathPolicyError):
            join_under_root("/vllm-workspace", "/vllm-workspace/src", "../../etc/passwd")

    def test_assert_under_root_allows_root_itself(self) -> None:
        self.assertEqual(assert_under_root("/vllm-workspace", "/vllm-workspace"), "/vllm-workspace")


if __name__ == "__main__":
    unittest.main()
