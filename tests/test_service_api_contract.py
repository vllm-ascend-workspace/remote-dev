from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from remote_dev import package_version
from remote_dev.mcp.server import handle


class PackageVersionContractTests(unittest.TestCase):
    def test_initialize_server_info_uses_installed_package_version(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"]["serverInfo"]["name"], "remote-dev")
        self.assertEqual(payload["result"]["serverInfo"]["version"], package_version())
        self.assertEqual(payload["result"]["serverInfo"]["version"], "0.2.0")


if __name__ == "__main__":
    unittest.main()
