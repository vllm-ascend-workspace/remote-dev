from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO_ROOT = ROOT.parent

import core.state_store as state_store  # noqa: E402
import mcp.tools as mcp_tools  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402
from mcp.schemas import ALIASES, TOOL_SCHEMAS  # noqa: E402
from mcp.tools import list_resources, list_tools, read_resource  # noqa: E402


class McpSchemaTests(unittest.TestCase):
    def test_all_expected_tools_are_listed(self) -> None:
        names = {tool["name"] for tool in list_tools()}
        self.assertEqual(names, set(TOOL_SCHEMAS))
        for expected in (
            "remote.read",
            "remote.write",
            "remote.edit",
            "remote.multi_edit",
            "remote.bash",
            "remote.glob",
            "remote.grep",
            "remote.ls",
            "remote.monitor",
            "remote.apply_patch",
            "remote.job_status",
            "remote.job_tail",
            "remote.job_stop",
            "remote.artifact_manifest",
            "remote.artifact_pull",
            "remote.artifact_push",
            "remote.context_snapshot",
            "remote.probe",
        ):
            self.assertIn(expected, names)
            self.assertIn("inputSchema", next(tool for tool in list_tools() if tool["name"] == expected))

    def test_underscore_aliases_map_to_canonical_names(self) -> None:
        self.assertEqual(ALIASES["remote_read"], "remote.read")
        self.assertIn("remote.bash", TOOL_SCHEMAS)

    def test_resources_include_endpoint_index(self) -> None:
        resources = {resource["uri"] for resource in list_resources()}
        self.assertIn("remote://endpoints", resources)
        content = read_resource("remote://endpoints")
        self.assertEqual(content["mimeType"], "application/json")
        self.assertIn("endpoints", json.loads(content["text"]))

    def test_resources_include_and_read_job_resources(self) -> None:
        original_state_root = state_store.substrate_root
        original_run_script = mcp_tools.run_script
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
                state_store.ensure_endpoint_state(endpoint)
                job_id = "job-abc123"
                record = {
                    "schema_version": "remote-dev.job.v1",
                    "job_id": job_id,
                    "target": endpoint.to_result_target(),
                    "remote_dir": f"{endpoint.root}/.remote-dev/jobs/{job_id}",
                    "started_at": "2026-05-25T00:00:00Z",
                }
                state_store.atomic_write_json(state_store.job_record_path(endpoint, job_id), record)
                mcp_tools.run_script = lambda *_args, **_kwargs: RemoteCompleted(0, "log\n", "")  # type: ignore[assignment]

                base = f"remote://endpoint/{endpoint.endpoint_id}/job/{job_id}"
                resources = {resource["uri"] for resource in list_resources()}
                self.assertIn(base + "/status", resources)
                self.assertIn(base + "/stdout", resources)
                self.assertIn(base + "/stderr", resources)

                status = read_resource(base + "/status")
                self.assertEqual(json.loads(status["text"])["job_id"], job_id)
                stdout = read_resource(base + "/stdout")
                self.assertEqual(stdout["mimeType"], "text/plain")
                self.assertEqual(stdout["text"], "log\n")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            mcp_tools.run_script = original_run_script  # type: ignore[assignment]

    def test_resources_include_and_read_artifact_manifest(self) -> None:
        original_state_root = state_store.substrate_root
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
                state_store.ensure_endpoint_state(endpoint)
                manifest = {
                    "schema_version": "remote-dev.artifact_manifest.v1",
                    "status": "ok",
                    "endpoint_id": endpoint.endpoint_id,
                    "file_count": 0,
                    "files": [],
                }
                artifact_id = "artifact-abc123"
                manifest_path = state_store.artifacts_dir(endpoint.endpoint_id) / artifact_id / "manifest.json"
                state_store.atomic_write_json(manifest_path, manifest)

                uri = f"remote://endpoint/{endpoint.endpoint_id}/artifacts/{artifact_id}/manifest"
                resources = {resource["uri"] for resource in list_resources()}
                self.assertIn(uri, resources)
                content = read_resource(uri)
                self.assertEqual(content["mimeType"], "application/json")
                self.assertEqual(json.loads(content["text"])["schema_version"], "remote-dev.artifact_manifest.v1")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]

    def test_context_snapshot_can_skip_live_probe(self) -> None:
        from mcp.tools import call_tool

        payload = call_tool(
            "remote.context_snapshot",
            {
                "host": "example.invalid",
                "port": 22,
                "root": "/vllm-workspace",
                "live_probe": False,
            },
        )
        self.assertEqual(payload["result"]["outcome"], "success")
        self.assertEqual(payload["result"]["tool"], "remote.context_snapshot")

    def test_server_supports_content_length_framing(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
        framed = b"Content-Length: " + str(len(encoded)).encode("ascii") + b"\r\n\r\n" + encoded
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / ".remote-dev" / "mcp" / "server.py")],
            input=framed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        header, body = proc.stdout.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length:", header)
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(response["id"], 1)
        self.assertIn("tools", response["result"])


if __name__ == "__main__":
    unittest.main()
