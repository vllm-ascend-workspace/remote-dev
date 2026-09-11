"""Portable local manifest keys when the client filesystem uses backslashes."""
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

from remote_dev.core.artifact_ops import _local_manifest, remote_artifact_push
from remote_dev.core.endpoint import Endpoint


def test_nested_local_paths_use_remote_slashes_and_preserve_bytes(tmp_path):
    root = tmp_path / "local 中文"
    file = root / "子目录" / "deeper" / "数据.bin"
    file.parent.mkdir(parents=True)
    data = bytes(range(256)) + b"\r\nLF\n"
    file.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert _local_manifest(root)["files"][0]["relpath"] == "子目录/deeper/数据.bin"
    observed = []
    def transfer(endpoint, command, **kwargs):
        observed.append((command, kwargs["stdin"]))
        return subprocess.CompletedProcess([], 0, (expected + "\n").encode(), b"")
    with patch("remote_dev.core.artifact_ops.run_bytes", side_effect=transfer):
        result = remote_artifact_push(Endpoint(host="192.0.2.1", port=22),
                                      local_path=str(root), remote_path="/tmp/upload")
    assert result["result"]["outcome"] == "success"
    assert "/tmp/upload/子目录/deeper/数据.bin" in observed[0][0]
    assert observed[0][1] == data
