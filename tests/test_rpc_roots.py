"""Real stdio worker checks: shared transport never changes job root ownership."""
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time

import pytest

from local_ssh import local_python_ssh
from remote_dev.core import rpc_transport
from remote_dev.core.cancellation import request_context
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.processes import control, worker_source


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real Linux supervisor requires /proc and prctl")


@pytest.fixture
def roots(tmp_path):
    rpc_transport.close_connections()
    endpoints = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        endpoints.append(Endpoint(host="fixture.invalid", port=46001, root=str(root), cwd=str(root)))
    with local_python_ssh():
        try:
            yield endpoints
        finally:
            for endpoint in endpoints:
                until(lambda: control(endpoint, "shared-job", "stop", force=True), lambda row: row["quiet"])
            rpc_transport.close_connections()


def spec(endpoint, command):
    return {"cwd": endpoint.root, "command": command, "env": {}, "timeout_seconds": 30}


def until(call, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        row = call()
        if predicate(row):
            return row
        assert time.monotonic() < deadline, row
        time.sleep(0.02)


def test_identical_job_ids_have_distinct_receipts_logs_gates_and_cached_modules(roots):
    a, b = roots
    prepared = [control(endpoint, "shared-job", "prepare", spec=spec(endpoint, "printf " + value))
                for endpoint, value in ((a, "alpha"), (b, "beta"))]
    assert len(rpc_transport._pool) == 1
    assert prepared[0]["receipt"]["pid"] != prepared[1]["receipt"]["pid"]
    assert prepared[0]["receipt"]["process_guard"]["marker"] != prepared[1]["receipt"]["process_guard"]["marker"]
    assert prepared[0]["remote_dir"] != prepared[1]["remote_dir"]
    assert prepared[1]["transport"]["connection_reused"]
    control(a, "shared-job", "go", authorization={"owner": "a"})
    assert control(b, "shared-job", "status")["state"] == "prepared"
    control(b, "shared-job", "go", authorization={"owner": "b"})
    for endpoint, text in ((a, "alpha"), (b, "beta")):
        done = control(endpoint, "shared-job", "exchange", yield_time_ms=3000, wait_for_exit=True)
        assert done["quiet"] and done["stdout"] == text
        assert done["result"]["exit_code"] == 0
    # Evict source/module entries with real worker revisions, alternating roots.
    # Re-loading the original module must rediscover each root's own receipts.
    for index in range(35):
        endpoint = roots[index % 2]
        row = rpc_transport.request(endpoint, "control", worker_source() + "\n# revision " + str(index),
                                    {"root": endpoint.root, "job_id": "shared-job", "action": "status"})
        assert row["receipt"]["pid"] == prepared[index % 2]["receipt"]["pid"]
    for endpoint, original in zip(roots, prepared):
        assert control(endpoint, "shared-job", "status")["receipt"] == original["receipt"]


def test_cwd_and_job_directory_symlinks_cannot_escape_into_a_sibling_root(roots):
    from pathlib import Path

    a, b = roots
    link = Path(a.root) / "escape"
    link.symlink_to(b.root, target_is_directory=True)
    for cwd in (b.root, str(link)):
        with pytest.raises(ValueError, match="cwd escapes"):
            control(a, "invalid-cwd", "prepare", spec={**spec(a, "touch escaped"), "cwd": cwd})
    jobs = Path(a.root) / ".remote-dev" / "jobs"
    (jobs / "escaped-job").symlink_to(b.root, target_is_directory=True)
    with pytest.raises(ValueError, match="job directory escapes"):
        control(a, "escaped-job", "status")
    assert not (Path(b.root) / "escaped").exists()
    assert control(b, "shared-job", "status")["state"] == "absent"


def test_cancelling_one_roots_exchange_drains_only_its_family(roots):
    a, b = roots
    for endpoint in roots:
        control(endpoint, "shared-job", "prepare", spec=spec(endpoint, "sleep 30 & wait"))
        control(endpoint, "shared-job", "go", authorization={"root": endpoint.root})
    before = control(b, "shared-job", "status")
    cancel = threading.Event()

    def observe_a():
        with request_context(cancel):
            return control(a, "shared-job", "exchange", yield_time_ms=20000, wait_for_exit=True)

    connection = next(iter(rpc_transport._pool.values())).connection
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(observe_a)
        until(lambda: len(connection.pending), lambda count: count == 1)
        try:
            live = control(b, "shared-job", "status")
            assert live["receipt"] == before["receipt"] and not live["quiet"]
            assert not waiting.done()
        finally:
            cancel.set()
        assert waiting.result(timeout=10)["quiet"]
    after = control(b, "shared-job", "status")
    assert after["state"] == "running" and after["receipt"] == before["receipt"]
    assert not after["quiet"] and len(rpc_transport._pool) == 1


def test_lost_shared_transport_reconnects_to_both_original_jobs_without_relaunch(roots):
    from pathlib import Path

    a, b = roots
    prepared = []
    for endpoint in roots:
        prepared.append(control(endpoint, "shared-job", "prepare",
                                spec=spec(endpoint, "printf '%s\\n' once >> launches; sleep 30 & wait")))
        control(endpoint, "shared-job", "go", authorization={"root": endpoint.root})
    for endpoint in roots:
        until(lambda: (Path(endpoint.root) / "launches").exists(), bool)
    connection = next(iter(rpc_transport._pool.values())).connection
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(control, a, "shared-job", "exchange", yield_time_ms=20000, wait_for_exit=True)
        until(lambda: len(connection.pending), lambda count: count == 1)
        connection.proc.kill()  # An abruptly lost channel cannot replay a submitted request.
        with pytest.raises(RemoteExecutionError, match="unknown"):
            waiting.result(timeout=5)
    for endpoint, original in zip(roots, prepared):
        recovered = control(endpoint, "shared-job", "status")
        assert recovered["state"] == "running" and not recovered["quiet"]
        assert recovered["receipt"] == original["receipt"]
        assert (Path(endpoint.root) / "launches").read_text() == "once\n"
    assert len(rpc_transport._pool) == 1
    assert next(iter(rpc_transport._pool.values())).connection is not connection
    until(lambda: control(a, "shared-job", "stop"), lambda row: row["quiet"])
    sibling = control(b, "shared-job", "status")
    assert sibling["state"] == "running" and sibling["receipt"] == prepared[1]["receipt"]
