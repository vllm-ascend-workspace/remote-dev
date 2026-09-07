from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .endpoint import Endpoint
from .errors import RemoteExecutionError

# ControlMaster socket directory. Consumers that already keep an OpenSSH mux
# directory for their own tooling can point remote-dev at it so both share
# one master connection per endpoint.
_MUX_DIR = Path(os.environ.get("REMOTE_DEV_SSH_MUX_DIR") or (Path.home() / ".ssh" / "remote-dev-mux")).expanduser()

# Decide mux-dir readiness once per process. None = undecided, True/False =
# usable / not usable.
_MUX_READY: bool | None = None

# Process-scoped multiplexing switch. Unset or exact "1" keeps the shared
# ControlMaster; exact "0" forces independent connections. Read on each
# invocation; never written back to os.environ or cached as a module global.
SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"


@dataclass
class RemoteCompleted:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def _control_master_options(identity_file: str | None = None) -> list[str]:
    """OpenSSH connection reuse through a ControlMaster socket directory.

    Prepared once per process. On failure we emit a single visible warning
    instead of silently disabling reuse (which reads as "the remote is slow").
    """
    global _MUX_READY
    if _MUX_READY is None:
        try:
            _MUX_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(_MUX_DIR, 0o700)
            _MUX_READY = True
        except OSError as exc:
            sys.stderr.write(
                f"[remote-dev] WARNING: SSH ControlMaster disabled; could not "
                f"prepare {_MUX_DIR} ({exc}). Remote tool calls will pay a fresh "
                f"SSH handshake each time. Fix ~/.ssh permissions to restore reuse.\n"
            )
            _MUX_READY = False
    if not _MUX_READY:
        return []
    # OpenSSH's %C hashes host/port/user but not the identity file; without a
    # per-key suffix two endpoints that differ only by SSH key would silently
    # share one master connection.
    key_suffix = ""
    if identity_file:
        key_suffix = "-" + hashlib.sha256(identity_file.encode("utf-8")).hexdigest()[:12]
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={_MUX_DIR}/%C{key_suffix}",
        "-o",
        "ControlPersist=120",
    ]


def _independent_ssh_connection_options() -> list[str]:
    # ControlMaster=no alone is not enough: a client can still attach to an
    # existing ControlPath. ControlPath=none blocks socket reuse, and
    # ControlPersist=no blocks inherited persistence.
    return [
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
    ]


def _shared_mux_requested() -> bool:
    """Read REMOTE_DEV_SSH_MUX without mutating os.environ or module globals."""
    value = os.environ.get(SSH_MUX_ENV)
    if value is None or value == "1":
        return True
    if value == "0":
        return False
    raise RemoteExecutionError(
        f"{SSH_MUX_ENV}={value!r} is not supported; accepted values are unset, "
        f"'1' (shared ControlMaster), or '0' (independent connections)"
    )


def ssh_base_cmd(endpoint: Endpoint) -> list[str]:
    if _shared_mux_requested():
        mux_options = _control_master_options(endpoint.identity_file)
    else:
        mux_options = _independent_ssh_connection_options()
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={max(1, int(endpoint.connect_timeout_ms / 1000))}",
        *mux_options,
    ]
    if endpoint.identity_file:
        cmd.extend(["-i", endpoint.identity_file])
    # User and host are never positional options. `-l` consumes `user`
    # even when it begins with `-`, and `--` stops option parsing before
    # `host`. `Endpoint.destination()` (`user@host`) is display-only.
    cmd.extend(["-l", endpoint.user, "-p", str(endpoint.port), "--", endpoint.host])
    return cmd


def run_script(endpoint: Endpoint, script: str, *, timeout_ms: int | None = None) -> RemoteCompleted:
    timeout = None if timeout_ms is None else timeout_ms / 1000
    try:
        proc = subprocess.run(
            [*ssh_base_cmd(endpoint), "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return RemoteCompleted(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_stream(exc.stdout)
        stderr = _decode_stream(exc.stderr)
        return RemoteCompleted(None, stdout, stderr, timed_out=True)


def run_bytes(
    endpoint: Endpoint,
    remote_command: str,
    *,
    stdin: bytes | None = None,
    timeout_ms: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    timeout = None if timeout_ms is None else timeout_ms / 1000
    return subprocess.run(
        [*ssh_base_cmd(endpoint), f"bash -c {shlex.quote(remote_command)}"],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def run_remote_python(
    endpoint: Endpoint,
    code: str,
    payload: dict[str, Any],
    *,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    timeout = None if timeout_ms is None else timeout_ms / 1000
    try:
        proc = subprocess.run(
            [*ssh_base_cmd(endpoint), f"python3 -c {shlex.quote(code)}"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": f"remote python timed out after {timeout_ms} ms",
            "stdout_tail": _decode_stream(exc.stdout)[-4000:],
            "stderr_tail": _decode_stream(exc.stderr)[-4000:],
        }
    if proc.returncode != 0:
        return {
            "status": "failed",
            "error": "remote python failed",
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
    try:
        data = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "error": f"remote python returned non-JSON: {exc}",
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
    return data if isinstance(data, dict) else {"status": "failed", "error": "remote python JSON was not an object"}


def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
