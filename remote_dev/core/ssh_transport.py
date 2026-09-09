from __future__ import annotations

import hashlib
import json
import os
import select
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .endpoint import Endpoint
from .errors import RemoteExecutionError

# ControlMaster socket directory. Consumers that already keep an OpenSSH mux
# directory for their own tooling can point remote-dev at it so both share
# one master connection per endpoint.
_MUX_DIR = Path(os.environ.get("REMOTE_DEV_SSH_MUX_DIR") or (Path.home() / ".ssh" / "remote-dev-mux")).expanduser()

# Decide mux-dir readiness once per process. None = undecided, True/False =
# usable / not usable.
_MUX_READY: bool | None = None

# Process-scoped multiplexing *default*. Unset or exact "1" keeps the shared
# ControlMaster; exact "0" forces independent connections. An endpoint's
# ``ssh_mux`` field overrides this for that endpoint only. Read on each
# invocation; never written back to os.environ or cached as a module global.
SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"

# Remote-side ``timeout(1)`` fires this many seconds before the local reader
# deadline so the remote process can exit with a useful status first.
REMOTE_TIMEOUT_GRACE_SECONDS = 5

# Local ``select`` slice. Small enough that a wall-clock deadline is honoured
# promptly, large enough that a quiet-but-alive stream is not spun on.
STREAM_SELECT_SLICE_SECONDS = 5.0

# Keepalive for endpoints that set the ``keepalive`` mechanism flag.
# Conditional: see ``_keepalive_options``. Hour-scale streams go through
# ``Endpoint.for_long_stream``, not this flag alone.
SERVER_ALIVE_INTERVAL_SECONDS = 30
SERVER_ALIVE_COUNT_MAX = 10

STREAM_MUX_REFUSAL = (
    "attached streams cannot use a multiplexed SSH connection: ControlMaster "
    "delegates -N forwards and hour-scale sessions to the mux master and the "
    "client exits rc=0 immediately, tearing the stream down. That failure is "
    "silent — rc=0 with the work gone. OpenSSH first-option-wins makes a later "
    "ControlMaster=no override ineffective, and ControlMaster=no alone is not "
    "enough because a client can still attach to an existing ControlPath. "
    "Use Endpoint.for_long_stream(...) or set ssh_mux=False before building "
    "the command."
)


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
    """Read REMOTE_DEV_SSH_MUX without mutating os.environ or module globals.

    This is the process-wide default only. Per-endpoint selection lives on
    ``Endpoint.ssh_mux`` and is applied by ``_uses_shared_mux``.
    """
    value = os.environ.get(SSH_MUX_ENV)
    if value is None or value == "1":
        return True
    if value == "0":
        return False
    raise RemoteExecutionError(
        f"{SSH_MUX_ENV}={value!r} is not supported; accepted values are unset, "
        f"'1' (shared ControlMaster), or '0' (independent connections)"
    )


def _uses_shared_mux(endpoint: Endpoint) -> bool:
    """Choose ControlMaster reuse for one endpoint, before argv is built.

    An explicit ``endpoint.ssh_mux`` wins. Otherwise the process-wide
    ``REMOTE_DEV_SSH_MUX`` default applies (unset or ``1`` shared, ``0``
    independent). The choice cannot be appended later: OpenSSH
    first-option-wins semantics make a trailing ``ControlMaster=no``
    override ineffective.

    Pass ``ssh_mux=False`` — or construct the endpoint with
    :meth:`Endpoint.for_long_stream` — for ``ssh -N -L`` tunnels and
    hour-scale attached streams. ControlMaster delegates ``-N`` forwards
    to the mux master and the client exits rc=0 immediately, tearing the
    tunnel down. That failure is silent — rc=0 with the tunnel gone. The
    same class of hang appears on hour-scale streams: the mux master
    stays up after the remote side has finished, and the attached client
    never notices. A later ``ControlMaster=no`` cannot fix this.
    """
    if endpoint.ssh_mux is not None:
        return bool(endpoint.ssh_mux)
    return _shared_mux_requested()


def _keepalive_options(endpoint: Endpoint) -> list[str]:
    """ServerAlive probes when the ``keepalive`` mechanism flag is set.

    This flag is orthogonal to mux: it only adds TCP probes. It does not
    mean "this is a long stream". Hour-scale streams and ``ssh -N -L``
    tunnels use :meth:`Endpoint.for_long_stream`, which sets
    ``ssh_mux=False`` and ``keepalive=True`` together.
    :func:`stream_ssh_command` refuses a multiplexed endpoint, because
    ControlMaster delegates ``-N`` forwards to the mux master and the
    client exits rc=0 immediately, tearing the tunnel down. That failure
    is silent; OpenSSH first-option-wins makes a later
    ``ControlMaster=no`` override ineffective.

    Conditional, not always-on. A connection carrying a slow-producing
    multi-hour job otherwise dies to an idle timeout somewhere in the path.
    Short multiplexed commands finish in seconds and do not sit idle;
    attaching ServerAlive to them would set TCP keepalive policy on the
    shared ControlMaster (the master owns the TCP connection, and OpenSSH
    first-option-wins makes the first client's ServerAlive the master's).
    """
    if not endpoint.keepalive:
        return []
    return [
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_SECONDS}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
    ]


def ssh_base_cmd(endpoint: Endpoint) -> list[str]:
    if _uses_shared_mux(endpoint):
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
        *_keepalive_options(endpoint),
    ]
    if endpoint.identity_file:
        cmd.extend(["-i", endpoint.identity_file])
    # User and host are never positional options. `-l` consumes `user`
    # even when it begins with `-`, and `--` stops option parsing before
    # `host`. `Endpoint.destination()` (`user@host`) is display-only.
    cmd.extend(["-l", endpoint.user, "-p", str(endpoint.port), "--", endpoint.host])
    return cmd


def stream_remote_payload(script: str, timeout_ms: int | None) -> str:
    """Wrap ``script`` in remote-side ``timeout --preserve-status`` when asked.

    The grace margin makes the remote killer fire first so the remote can
    report something useful before the local reader gives up.
    ``--preserve-status`` keeps a successful command's real exit code.
    """
    if timeout_ms is None or timeout_ms <= 0:
        return script
    timeout_s = timeout_ms / 1000
    margin = max(int(timeout_s) - REMOTE_TIMEOUT_GRACE_SECONDS, 1)
    return f"timeout --preserve-status {margin}s bash -lc {shlex.quote(script)}"


def _require_independent_stream(endpoint: Endpoint) -> None:
    """Refuse a multiplexed connection for an attached stream.

    The failure this guards is silent: ControlMaster delegates ``-N``
    forwards and hour-scale sessions to the mux master, the client exits
    rc=0, and the tunnel or stream is gone. OpenSSH first-option-wins
    makes a later ``ControlMaster=no`` override ineffective, so the
    independent triple has to be chosen before argv is built.
    ``ControlMaster=no`` alone is not enough — a client can still attach
    to an existing ``ControlPath``. A docstring is not a control for a
    failure that announces success.
    """
    if _uses_shared_mux(endpoint):
        raise RemoteExecutionError(STREAM_MUX_REFUSAL)


def stream_ssh_command(endpoint: Endpoint, script: str, *, timeout_ms: int | None = None) -> list[str]:
    """Argv for an attached live-stream SSH invocation.

    Refuses a multiplexed endpoint. Use :meth:`Endpoint.for_long_stream`
    or pass ``ssh_mux=False``.
    """
    _require_independent_stream(endpoint)
    return [*ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(stream_remote_payload(script, timeout_ms))]


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


def run_stream(
    endpoint: Endpoint,
    script: str,
    *,
    timeout_ms: int | None = None,
    forward_prefix: str = "[remote] ",
    output: TextIO | None = None,
) -> RemoteCompleted:
    """Run a remote command, forwarding stdout/stderr live as they arrive.

    This is a transport primitive. It returns :class:`RemoteCompleted` with
    the remote exit code (or ``timed_out=True``). Live bytes are written to
    ``output`` (default ``stderr``) and are not stuffed into
    ``RemoteCompleted.stdout``. It does **not** emit ``remote-dev.result.v1``;
    that contract is the result of one finished tool call. A later tool
    wrapper that calls this should wrap the completed invocation in
    ``make_result``.

    Not ``remote.job_*``. Jobs are detached (``nohup``), persist a job dir,
    and ``job_tail`` snapshots log files through ``run_script``. An attached
    stream is required when an agent must see stage progress as it happens
    and must tell a hang from slow progress. Detach-and-tail leaves both
    holes: no remote-side kill of the original command, and no local
    wall-clock kill while a pipe is stalled.

    Silent-hang handling: ``timeout_ms`` is enforced two ways at once.

    1. Remote-side kill. The command is wrapped in
       ``timeout --preserve-status <s>s bash -lc …`` so an unresponsive
       remote process is killed at the source even when it has stopped
       producing output. A five-second grace margin lets the remote timeout
       fire first. ``--preserve-status`` keeps a successful command's real
       exit code.
    2. Local-side kill. The local reader uses ``select.select`` with a
       small slice so a wall-clock timeout is honoured immediately even
       when output is sitting in a slow pipe buffer.

    Either alone leaves a hole: remote-only misses a dead network;
    local-only leaves an orphan process burning an NPU.

    Callers that stream hour-scale jobs construct the endpoint with
    :meth:`Endpoint.for_long_stream`. :func:`stream_ssh_command` refuses
    a multiplexed endpoint: ControlMaster delegates the session to the
    mux master and the client exits rc=0, which looks like success.
    """
    dest = sys.stderr if output is None else output
    cmd = stream_ssh_command(endpoint, script, timeout_ms=timeout_ms)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    return _read_stream(proc, timeout_ms=timeout_ms, forward_prefix=forward_prefix, output=dest)


def _read_stream(
    proc: subprocess.Popen,
    *,
    timeout_ms: int | None,
    forward_prefix: str,
    output: TextIO,
) -> RemoteCompleted:
    """Local reader half of silent-hang handling. ``proc.stdout`` is required."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    started = time.monotonic()
    deadline = None if timeout_ms is None or timeout_ms <= 0 else started + (timeout_ms / 1000)
    timed_out = False
    try:
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    timed_out = True
                    return RemoteCompleted(
                        None,
                        "",
                        f"remote command exceeded {timeout_ms} ms wall-clock limit",
                        timed_out=True,
                    )
                wait = min(remaining, STREAM_SELECT_SLICE_SECONDS)
            else:
                wait = STREAM_SELECT_SLICE_SECONDS
            ready, _, _ = select.select([fd], [], [], wait)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                output.write(forward_prefix + line if not line.startswith(forward_prefix) else line)
                output.flush()
            elif proc.poll() is not None:
                remainder = proc.stdout.read()
                if remainder:
                    output.write(forward_prefix + remainder if not remainder.startswith(forward_prefix) else remainder)
                    output.flush()
                break
        returncode = proc.wait()
        return RemoteCompleted(returncode, "", "", timed_out=timed_out)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


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
