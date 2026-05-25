from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
import core.state_store as state_store  # noqa: E402


class ReadLedgerTests(unittest.TestCase):
    def test_read_ledger_round_trip_uses_endpoint_state(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            original = state_store.substrate_root
            try:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                path = state_store.write_read_ledger(
                    endpoint,
                    {
                        "path": "/vllm-workspace/foo.py",
                        "sha256": "abc",
                        "size": 3,
                        "mtime_ns": 1,
                        "offset": 1,
                        "limit": 200,
                    },
                )
                self.assertTrue(path.exists())
                loaded = state_store.load_read_ledger(endpoint, "/vllm-workspace/foo.py")
                self.assertEqual(loaded["sha256"], "abc")
                self.assertIn(endpoint.endpoint_id, str(path))
            finally:
                state_store.substrate_root = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
