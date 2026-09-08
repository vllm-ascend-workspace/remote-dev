from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import SERVICE_API_VERSION  # noqa: E402
from mcp.server import handle  # noqa: E402


class ServiceApiContractTests(unittest.TestCase):
    def test_service_api_json_matches_advertised_version(self) -> None:
        contract = json.loads((ROOT / "service-api.json").read_text())
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["name"], "remote-dev")
        self.assertIn(contract["service_api_version"], contract["supports"])
        self.assertEqual(contract["service_api_version"], SERVICE_API_VERSION)

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        advertised = json.loads(stdout.getvalue())["result"]["capabilities"]["experimental"]["remote-dev"][
            "service_api_version"
        ]
        self.assertEqual(advertised, SERVICE_API_VERSION)


if __name__ == "__main__":
    unittest.main()
