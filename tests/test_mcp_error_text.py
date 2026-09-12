from __future__ import annotations

import unittest
from unittest.mock import patch

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.search_ops import remote_grep
from remote_dev.mcp import server


class McpErrorTextTests(unittest.TestCase):
    def test_sparse_failure_reaches_text_only_client_with_recovery_detail(self) -> None:
        detail = "grep fallback cannot honor --type py; install ripgrep (rg) or use --glob"
        payload = {"text": "\n", "result": {"outcome": "failed", "status": "rg_required", "error": detail}}
        with patch.object(server, "call_tool", return_value=payload), \
                patch.object(server, "runtime_status", return_value={}), patch.object(server, "send") as send:
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "remote_grep"}})
        result = send.call_args.args[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("rg_required", result["content"][0]["text"])
        self.assertIn(detail, result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["error"], detail)

    def test_failed_search_is_not_reported_as_zero_matches(self) -> None:
        detail = "grep fallback cannot honor --type py; install ripgrep (rg) or use --glob"
        with patch("remote_dev.core.search_ops.run_remote_python", return_value={"status": "rg_required", "error": detail}):
            payload = remote_grep(Endpoint(host="example.invalid", port=22), pattern="timeout", type="py")
        self.assertNotIn("found 0 matches", payload["result"]["summary"])
        self.assertIn(detail, payload["text"])
        self.assertEqual(server.tool_text(payload).count(detail), 1)

    def test_success_and_cancelled_text_remain_unchanged(self) -> None:
        for outcome in ("success", "cancelled"):
            self.assertEqual(server.tool_text({"text": "bounded output\n", "result": {"outcome": outcome}}), "bounded output\n")
