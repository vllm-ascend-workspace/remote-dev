"""The local pipe must preserve the bytes of a Linux shell script."""
import sys
from unittest import mock

from remote_dev.core.endpoint import Endpoint
from remote_dev.core import ssh_transport


def test_script_pipe_preserves_lf_and_utf8():
    script = "set -e\nprintf '中文\\n'\ncat <<'END'\nliteral $data\nEND\n"
    reader = [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"]
    with mock.patch.object(ssh_transport, "ssh_base_cmd", return_value=reader):
        result = ssh_transport.run_script(Endpoint(host="example.invalid", port=22), script)
    assert result.returncode == 0
    assert bytes.fromhex(result.stdout.strip()) == script.encode("utf-8")
