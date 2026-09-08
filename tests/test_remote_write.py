from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402


class RemoteWriteTests(unittest.TestCase):
    def test_remote_write_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = file_ops.remote_write(endpoint, file_path="/tmp/outside.txt", content="x")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")


if __name__ == "__main__":
    unittest.main()
