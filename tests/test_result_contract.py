from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.result import make_result  # noqa: E402


class ResultContractTests(unittest.TestCase):
    def test_make_result_uses_canonical_schema(self) -> None:
        result = make_result(
            tool="remote.bash",
            target={"kind": "direct-endpoint", "endpoint_id": "abc"},
            outcome="success",
            status="ok",
            summary="done",
        )
        self.assertEqual(result["schema_version"], "remote-dev.result.v1")
        self.assertEqual(result["tool"], "remote.bash")
        self.assertEqual(result["outcome"], "success")
        self.assertIn("invocation_id", result)
        self.assertEqual(result["changed_files"], [])


if __name__ == "__main__":
    unittest.main()
