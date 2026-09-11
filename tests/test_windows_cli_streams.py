"""UTF-8 CLI pipes must not depend on the Windows console code page."""
import json
import os
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == "nt", "native Windows console encoding")
class CliStreams(unittest.TestCase):
    def test_json_stdin_and_stdout_are_utf8_without_connecting(self):
        data = dict(host="127.0.0.1", port=9, user="test", root="/sandbox", cwd="/sandbox",
                    runtime_env=False, file_path="../中文-🙂.txt")
        process = subprocess.run(
            [sys.executable, "-m", "remote_dev", "read", "--input-json", "-"],
            input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "cp936"},
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        result = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(result["result"]["status"], "path_outside_root")
        self.assertIn("🙂", json.dumps(result, ensure_ascii=False))

    def test_mcp_json_lines_preserve_utf8_without_connecting(self):
        data = dict(host="127.0.0.1", port=9, user="test", root="/sandbox", cwd="/sandbox",
                    runtime_env=False, file_path="../中文-🙂.txt")
        request = dict(jsonrpc="2.0", id=1, method="tools/call",
                       params=dict(name="remote.read", arguments=data))
        process = subprocess.run(
            [sys.executable, "-m", "remote_dev.mcp.server"],
            input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "cp936"},
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout.decode("utf-8"))["result"]
        self.assertEqual(result["structuredContent"]["status"], "path_outside_root")
        self.assertIn("🙂", json.dumps(result, ensure_ascii=False))
