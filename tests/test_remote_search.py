from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
import core.search_ops as search_ops  # noqa: E402


class RemoteSearchTests(unittest.TestCase):
    def test_remote_glob_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        payload = search_ops.remote_glob(endpoint, pattern="*", path="/etc")
        self.assertEqual(payload["result"]["outcome"], "blocked")

    def test_remote_grep_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        payload = search_ops.remote_grep(endpoint, pattern="x", path="/etc")
        self.assertEqual(payload["result"]["outcome"], "blocked")


if __name__ == "__main__":
    unittest.main()
