import os
import pytest
from unittest.mock import patch
import socket
import urllib.error
import urllib.request

from remote_dev.core.endpoint import resolve_endpoint
from remote_dev.core.ssh_transport import RemoteCompleted
from remote_dev.diagnostics import diagnose_ssh, http_connection, http_failure, open_http
from remote_dev.runtime import process_identity, runtime_status


@pytest.mark.skipif(os.name == "nt", reason="ControlMaster is unsupported by Win32 OpenSSH")
def test_mux_probe_never_replays_caller_command():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "root": "/", "ssh_mux": True})
    with patch("remote_dev.diagnostics.run_script", side_effect=[
        RemoteCompleted(255, "", "timeout", timed_out=True),
        RemoteCompleted(0, "remote-dev-connection-ok\n", "", timed_out=False),
    ]) as run:
        result = diagnose_ssh(endpoint)
    assert result["status"] == "independent_connection_works"
    assert result["business_command_replayed"] is False
    assert run.call_args_list[1].args[0].ssh_mux is False
    assert all(call.args[1] == "printf 'remote-dev-connection-ok\\n'" for call in run.call_args_list)


def test_successful_probe_needs_no_second_connection():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22})
    with patch("remote_dev.diagnostics.run_script", return_value=RemoteCompleted(0, "remote-dev-connection-ok", "", False)) as run:
        assert diagnose_ssh(endpoint)["status"] == "ok"
    assert run.call_count == 1


def test_http_direct_is_explicit_and_errors_are_not_model_attribution():
    with patch("urllib.request.build_opener") as build:
        open_http("http://example.test/health", timeout=5)
        assert build.call_args.args[0].proxies == {}
        build.return_value.open.assert_called_once_with("http://example.test/health", timeout=5)
    assert http_connection("http://u:password@example.test/health?token=secret")["target"] == "http://example.test/health"
    assert http_failure(urllib.error.HTTPError("http://example.test", 502, "bad gateway", {}, None)) == {"kind": "http_status", "status_code": 502}
    assert http_failure(urllib.error.URLError(socket.gaierror("lookup")))["kind"] == "dns"


def test_loaded_identity_does_not_change_when_installation_changes():
    first = {"package": "pkg", "version": "1", "commit": "a", "location": "/env"}
    with patch("remote_dev.runtime.installed_identity", return_value=first):
        loaded = process_identity("pkg")
    with patch("remote_dev.runtime.installed_identity", return_value={**first, "commit": "b"}):
        status = runtime_status(loaded)
    assert status["status"] == "restart_required"
    assert status["loaded"]["commit"] == "a"
    assert status["installed"]["commit"] == "b"
