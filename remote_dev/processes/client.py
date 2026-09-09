"""Client boundary for generic remote process supervision.

``remote_dev.processes.control(endpoint, job_id, action, **parameters)`` is
the package contract. Coordinator and ordinary background jobs share this
path. The Linux worker is shipped as source text over the existing SSH
Python payload transport (argv list + JSON stdin). The local client does
not invoke Bash or PowerShell to quote that payload.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from remote_dev.core.endpoint import Endpoint, resolve_endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.ssh_transport import run_remote_python

ACTIONS = frozenset({"prepare", "go", "status", "tail", "stop"})
CONTROL_TIMEOUT_MS = 45000
WORKER_RELATIVE = Path(__file__).with_name("worker.py")

# Small remote bootstrap: load the JSON payload, exec the shipped worker,
# and always print one JSON object. Application errors stay on stdout so
# the transport can return them without a local shell wrapper.
_CONTROL_BOOTSTRAP = """
import json, sys
payload = json.load(sys.stdin)
ns = {}
exec(compile(payload["worker_source"], "<remote-dev-worker>", "exec"), ns, ns)
try:
    result = ns["control_job"](payload["request"], payload["worker_source"])
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}))
    raise SystemExit(0)
print(json.dumps({"ok": True, "result": result}, default=str))
"""

_REMOTE_EXCEPTIONS = {
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
    "FileNotFoundError": FileNotFoundError,
    "NotADirectoryError": NotADirectoryError,
    "OSError": OSError,
}


def worker_source() -> str:
    """Return the Linux supervisor source shipped into the remote root."""
    path = WORKER_RELATIVE
    if not path.is_file():
        raise RuntimeError(f"this install is missing {path.name}")
    return path.read_text(encoding="utf-8")


def _as_endpoint(endpoint: Endpoint | Mapping[str, Any]) -> Endpoint:
    if isinstance(endpoint, Endpoint):
        return endpoint
    if isinstance(endpoint, Mapping):
        return resolve_endpoint(dict(endpoint))
    raise TypeError("endpoint must be an Endpoint or mapping")


def _raise_remote(data: dict[str, Any]) -> None:
    message = str(data.get("error") or "process control failed")
    name = str(data.get("type") or "RemoteExecutionError")
    exc_type = _REMOTE_EXCEPTIONS.get(name, RemoteExecutionError)
    raise exc_type(message)


def control(endpoint: Endpoint | Mapping[str, Any], job_id: str, action: str, **parameters: Any) -> dict[str, Any]:
    """Run one generic process-control action on an explicit endpoint.

    ``endpoint`` is an :class:`~remote_dev.core.endpoint.Endpoint` or an
    ordinary connection mapping (``host`` + ``port`` plus auth/root/cwd).
    ``action`` is ``prepare``, ``go``, ``status``, ``tail``, or ``stop``.
    ``prepare`` takes ``spec={"command", "cwd", "env", "timeout_seconds"}``.
    ``go`` may carry an opaque ``authorization`` value; this package does
    not interpret resource-lease or fence semantics. Return value is the
    structured supervisor dict (``state``, ``quiet``, ``receipt``, ...).
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported job action: {action}")
    target = _as_endpoint(endpoint)
    request = {"root": target.root, "job_id": job_id, "action": action, **parameters}
    payload = {"request": request, "worker_source": worker_source()}
    data = run_remote_python(target, _CONTROL_BOOTSTRAP, payload, timeout_ms=CONTROL_TIMEOUT_MS)
    if not isinstance(data, dict):
        raise RemoteExecutionError("process control returned a non-object")
    if data.get("status") == "timeout":
        raise RemoteExecutionError(data.get("error") or "process control timed out")
    if data.get("status") == "failed":
        detail = data.get("stderr_tail") or data.get("error") or "process control failed"
        raise RemoteExecutionError(str(detail)[-2000:])
    if data.get("ok") is False:
        _raise_remote(data)
    if data.get("ok") is True:
        result = data.get("result")
        if not isinstance(result, dict):
            raise RemoteExecutionError("process control result was not an object")
        return result
    # Direct supervisor dict (tests and any bootstrap that prints it raw).
    if "state" in data:
        return data
    raise RemoteExecutionError("process control returned unrecognized JSON: " + json.dumps(data)[:500])
