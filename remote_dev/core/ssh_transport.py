from __future__ import annotations

import codecs
import hashlib
import json
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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

INTERACTIVE_MUX_REFUSAL = (
    "interactive bootstrap cannot use a multiplexed SSH connection: a "
    "password prompt through a ControlMaster is meaningless and hangs. "
    "It is impossible to combine BatchMode=no with multiplexing. Pass "
    "ssh_mux=False on the endpoint (do not attach this session to a mux "
    "master)."
)

# OpenSSH -N through a mux master exits 0 while the tunnel is gone. A dead
# forward is never reported as success.
FORWARD_DEAD_EXIT_CODE = 255


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


def _ssh_cmd(endpoint: Endpoint, option_tokens: Sequence[str] = ()) -> list[str]:
    """Compose an SSH argv. ``option_tokens`` are placed before ``--``.

    ``--`` stops OpenSSH option parsing. Anything after the host is a
    remote command, not an option: ``-N`` / ``-L`` / ``-o`` appended past
    the destination are executed on the far side and never take effect.
    This helper is the only place that emits ``--``, so package callers
    cannot reintroduce that split. ``option_tokens`` is not a public
    extra-options escape hatch; only this module passes tokens it owns.
    """
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
        *option_tokens,
    ]
    if endpoint.identity_file:
        cmd.extend(["-i", endpoint.identity_file])
    # User and host are never positional options. `-l` consumes `user`
    # even when it begins with `-`, and `--` stops option parsing before
    # `host`. `Endpoint.destination()` (`user@host`) is display-only.
    cmd.extend(["-l", endpoint.user, "-p", str(endpoint.port), "--", endpoint.host])
    return cmd


def ssh_base_cmd(endpoint: Endpoint) -> list[str]:
    return _ssh_cmd(endpoint)


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
    on_output: Callable[[str, str], None] | None = None,
    merge_stderr: bool = True,
) -> RemoteCompleted:
    """Run a remote command, forwarding stdout/stderr live as they arrive.

    This is a transport primitive. It returns :class:`RemoteCompleted` with
    the remote exit code (or ``timed_out=True``). By default, live bytes are
    written to ``output`` (default ``stderr``) and are not stuffed into
    ``RemoteCompleted.stdout``. It does **not** emit ``remote-dev.result.v1``;
    that contract is the result of one finished tool call. A later tool
    wrapper that calls this should wrap the completed invocation in
    ``make_result``.

    ``on_output(channel, text)`` is a generic line callback. ``channel`` is
    ``"stdout"`` or ``"stderr"``. This module does not parse application
    sentinels.

    Default ``merge_stderr=True`` keeps the historical merged-stream
    behaviour: stdout and stderr are joined, forwarded with
    ``forward_prefix``, and ``RemoteCompleted.stdout`` / ``.stderr`` stay
    empty (except the timeout message). Pass ``merge_stderr=False`` to keep
    the channels separate, capture both, and invoke ``on_output`` per line.
    Separate-channel mode does not auto-forward unless ``output`` is set.

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
    dest = sys.stderr if output is None and merge_stderr else output
    cmd = stream_ssh_command(endpoint, script, timeout_ms=timeout_ms)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        bufsize=0,
    )
    return _read_attached(
        proc,
        timeout_ms=timeout_ms,
        forward_prefix=forward_prefix,
        output=dest,
        on_output=on_output,
        capture=not merge_stderr,
    )


_STREAM_READ_BYTES = 4096


def _read_fd(fd: int) -> bytes | None:
    """Read available bytes. ``None`` means EOF; ``b''`` means try again."""
    try:
        chunk = os.read(fd, _STREAM_READ_BYTES)
    except BlockingIOError:
        return b""
    except OSError:
        return None
    return chunk if chunk else None


@dataclass
class _StreamChannel:
    name: str
    fd: int
    decoder: Any = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    pending: str = ""
    captured: list[str] = field(default_factory=list)


def _emit_stream_text(
    text: str,
    *,
    channel: str,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    captured: list[str] | None,
) -> None:
    if not text:
        return
    if on_output is not None:
        on_output(channel, text)
    if captured is not None:
        captured.append(text)
    if output is None:
        return
    output.write(forward_prefix + text if forward_prefix and not text.startswith(forward_prefix) else text)
    output.flush()


def _flush_channel_lines(
    channel: _StreamChannel,
    *,
    text: str,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    capture: bool,
    final: bool,
) -> None:
    channel.pending += text
    while True:
        newline = channel.pending.find("\n")
        if newline < 0:
            break
        line = channel.pending[: newline + 1]
        channel.pending = channel.pending[newline + 1 :]
        _emit_stream_text(
            line,
            channel=channel.name,
            forward_prefix=forward_prefix,
            output=output,
            on_output=on_output,
            captured=channel.captured if capture else None,
        )
    if final and channel.pending:
        _emit_stream_text(
            channel.pending,
            channel=channel.name,
            forward_prefix=forward_prefix,
            output=output,
            on_output=on_output,
            captured=channel.captured if capture else None,
        )
        channel.pending = ""


def _reap_process(proc: subprocess.Popen[bytes], *, timeout_s: float | None = 2.0) -> int | None:
    if proc.poll() is not None:
        return proc.returncode
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> int | None:
    if proc.poll() is None:
        proc.kill()
    if proc.poll() is not None:
        return proc.returncode
    try:
        return proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        return proc.poll()


def _close_pipe(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _read_attached(
    proc: subprocess.Popen[bytes],
    *,
    timeout_ms: int | None,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    capture: bool,
) -> RemoteCompleted:
    """Deadline-aware reader for merged or separate SSH streams.

    Uses ``select`` + ``os.read`` so a partial line cannot block past the
    wall-clock deadline. ``capture=False`` is the historical merged mode
    (forward live, leave ``RemoteCompleted.stdout`` empty).
    """
    assert proc.stdout is not None
    channels = [_StreamChannel(name="stdout", fd=proc.stdout.fileno())]
    if proc.stderr is not None and proc.stderr is not proc.stdout:
        channels.append(_StreamChannel(name="stderr", fd=proc.stderr.fileno()))
    by_fd = {channel.fd: channel for channel in channels}
    open_fds = set(by_fd)
    for fd in open_fds:
        os.set_blocking(fd, False)
    started = time.monotonic()
    deadline = None if timeout_ms is None or timeout_ms <= 0 else started + (timeout_ms / 1000)
    timeout_message = (
        f"remote command exceeded {timeout_ms} ms wall-clock limit" if timeout_ms is not None else ""
    )

    def captured_stdout() -> str:
        stdout = next(channel for channel in channels if channel.name == "stdout")
        return "".join(stdout.captured)

    def captured_stderr() -> str:
        stderr = next((channel for channel in channels if channel.name == "stderr"), None)
        return "" if stderr is None else "".join(stderr.captured)

    def finalize_open_channels() -> None:
        for channel in channels:
            leftover = ""
            try:
                leftover = channel.decoder.decode(b"", final=True)
            except Exception:
                leftover = ""
            _flush_channel_lines(
                channel,
                text=leftover,
                forward_prefix=forward_prefix,
                output=output,
                on_output=on_output,
                capture=capture,
                final=True,
            )

    def timed_out_result() -> RemoteCompleted:
        finalize_open_channels()
        stderr = timeout_message
        if capture:
            extra = captured_stderr()
            if extra:
                if not extra.endswith("\n") and timeout_message:
                    extra = extra + "\n"
                stderr = extra + timeout_message
        return RemoteCompleted(
            None,
            captured_stdout() if capture else "",
            stderr,
            timed_out=True,
        )

    def close_channel(channel: _StreamChannel) -> None:
        stream = proc.stdout if channel.name == "stdout" else proc.stderr
        _close_pipe(stream)

    def close_pipes() -> None:
        seen: set[int] = set()
        for stream in (proc.stdout, proc.stderr):
            if stream is None or id(stream) in seen:
                continue
            seen.add(id(stream))
            _close_pipe(stream)

    def remaining_wait() -> float | None:
        if deadline is None:
            return STREAM_SELECT_SLICE_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(remaining, STREAM_SELECT_SLICE_SECONDS)

    try:
        while True:
            wait = remaining_wait()
            if wait is not None and wait <= 0:
                _kill_and_reap(proc)
                return timed_out_result()
            if not open_fds:
                # Pipes have closed. Wait until the real deadline (or forever
                # if none). A select-slice TimeoutExpired must not kill here:
                # grandchildren can close stdout/stderr while the child still
                # has work left.
                if proc.poll() is not None:
                    returncode = proc.wait()
                    return RemoteCompleted(
                        returncode,
                        captured_stdout() if capture else "",
                        captured_stderr() if capture else "",
                    )
                if deadline is None:
                    returncode = proc.wait()
                    return RemoteCompleted(
                        returncode,
                        captured_stdout() if capture else "",
                        captured_stderr() if capture else "",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_and_reap(proc)
                    return timed_out_result()
                try:
                    returncode = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    _kill_and_reap(proc)
                    return timed_out_result()
                return RemoteCompleted(
                    returncode,
                    captured_stdout() if capture else "",
                    captured_stderr() if capture else "",
                )
            ready, _, _ = select.select(list(open_fds), [], [], wait)
            if ready:
                for fd in ready:
                    channel = by_fd[fd]
                    chunk = _read_fd(fd)
                    if chunk is None:
                        _flush_channel_lines(
                            channel,
                            text=channel.decoder.decode(b"", final=True),
                            forward_prefix=forward_prefix,
                            output=output,
                            on_output=on_output,
                            capture=capture,
                            final=True,
                        )
                        open_fds.discard(fd)
                        close_channel(channel)
                        continue
                    if not chunk:
                        continue
                    _flush_channel_lines(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                continue
            if proc.poll() is None:
                continue
            for fd in list(open_fds):
                channel = by_fd[fd]
                while True:
                    chunk = _read_fd(fd)
                    if not chunk:
                        break
                    _flush_channel_lines(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                _flush_channel_lines(
                    channel,
                    text=channel.decoder.decode(b"", final=True),
                    forward_prefix=forward_prefix,
                    output=output,
                    on_output=on_output,
                    capture=capture,
                    final=True,
                )
                open_fds.discard(fd)
                close_channel(channel)
            returncode = proc.wait()
            return RemoteCompleted(
                returncode,
                captured_stdout() if capture else "",
                captured_stderr() if capture else "",
            )
    finally:
        close_pipes()
        if proc.poll() is None:
            _kill_and_reap(proc)


def _read_stream(
    proc: subprocess.Popen[bytes],
    *,
    timeout_ms: int | None,
    forward_prefix: str,
    output: TextIO,
    on_output: Callable[[str, str], None] | None = None,
) -> RemoteCompleted:
    """Merged-stream wrapper around :func:`_read_attached` for existing tests."""
    return _read_attached(
        proc,
        timeout_ms=timeout_ms,
        forward_prefix=forward_prefix,
        output=output,
        on_output=on_output,
        capture=False,
    )


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


# ---------------------------------------------------------------------------
# Local port forward (ssh -N -L) and interactive bootstrap
# ---------------------------------------------------------------------------


def _rewrite_forward_exit(returncode: int | None) -> int:
    """A dead ``-N`` forward is never success. Mux-absorbed clients exit 0."""
    if returncode is None or returncode == 0:
        return FORWARD_DEAD_EXIT_CODE
    return int(returncode)


def _validate_forward_host(value: str, *, field: str) -> str:
    host = str(value)
    if not host or host.startswith("-"):
        raise RemoteExecutionError(f"{field} must be a hostname, not an option: {value!r}")
    if any(char in host for char in ("\n", "\r", "\0", " ")):
        raise RemoteExecutionError(f"{field} contains invalid characters: {value!r}")
    return host


def _validate_port(value: int, *, field: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteExecutionError(f"{field} must be an integer") from exc
    if isinstance(value, bool) or not (1 <= port <= 65535):
        raise RemoteExecutionError(f"{field} must be in 1..65535, got {value!r}")
    return port


def _find_free_local_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _as_long_stream(endpoint: Endpoint) -> Endpoint:
    """Normalize to the for_long_stream shape after refusing a muxed endpoint."""
    _require_independent_stream(endpoint)
    if endpoint.keepalive:
        return endpoint
    return Endpoint.for_long_stream(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        root=endpoint.root,
        cwd=endpoint.cwd,
        runtime_env=endpoint.runtime_env,
        runtime_env_file=endpoint.runtime_env_file,
        identity_file=endpoint.identity_file,
        connect_timeout_ms=endpoint.connect_timeout_ms,
        kind=endpoint.kind,
        alias=endpoint.alias,
        source=endpoint.source,
    )


def local_forward_ssh_command(
    endpoint: Endpoint,
    *,
    local_host: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
) -> list[str]:
    """Argv for ``ssh -N -L`` on the ``for_long_stream`` shape.

    Refuses a multiplexed endpoint. Always includes ``ControlMaster=no``,
    keepalives, ``ExitOnForwardFailure=yes``, and ``-N``. Callers cannot
    inject extra ``-o`` strings.
    """
    endpoint = _as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    local_port = _validate_port(local_port, field="local_port")
    remote_port = _validate_port(remote_port, field="remote_port")
    return _ssh_cmd(
        endpoint,
        (
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            f"{local_host}:{local_port}:{remote_host}:{remote_port}",
        ),
    )


def _stop_process_group(proc: subprocess.Popen[Any], *, timeout_s: float = 5.0) -> int:
    if proc.poll() is not None:
        return _rewrite_forward_exit(proc.returncode)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        return _rewrite_forward_exit(proc.wait(timeout=timeout_s))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        return _rewrite_forward_exit(proc.wait(timeout=timeout_s))


class LocalForward:
    """Handle for one local→remote SSH port forward.

    ``local_port`` is the bound loopback port. ``wait_ready`` blocks until
    that port accepts connections. ``close`` kills the process group so no
    child is left behind. ``poll`` and ``close`` never report rc=0 for a
    dead forward.
    """

    def __init__(
        self,
        *,
        proc: subprocess.Popen[Any],
        local_host: str,
        local_port: int,
        remote_host: str,
        remote_port: int,
    ) -> None:
        self.local_host = local_host
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._proc = proc
        self._stderr = ""
        self._stderr_read = False
        self._closed = False

    def _consume_stderr(self) -> str:
        if self._stderr_read:
            return self._stderr
        self._stderr_read = True
        if self._proc.stderr is None:
            self._stderr = ""
            return self._stderr
        data = self._proc.stderr.read() or ""
        self._stderr = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        return self._stderr

    def poll(self) -> int | None:
        rc = self._proc.poll()
        if rc is None:
            return None
        return _rewrite_forward_exit(rc)

    def wait_ready(self, timeout_s: float = 15.0) -> None:
        """Block until ``local_port`` accepts connections.

        Raises :class:`RemoteExecutionError` if the process dies or the
        timeout expires. A process that exits 0 is reported as
        :data:`FORWARD_DEAD_EXIT_CODE`.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        last_error = ""
        family = socket.AF_INET6 if ":" in self.local_host else socket.AF_INET
        while time.monotonic() < deadline:
            rc = self._proc.poll()
            if rc is not None:
                stderr = self._consume_stderr()
                rewritten = _rewrite_forward_exit(rc)
                detail = f"rc={rewritten}"
                if rc != rewritten:
                    detail += f", ssh rc={rc}"
                raise RemoteExecutionError(
                    f"ssh local forward exited early ({detail}): {stderr[:2000]}"
                )
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.5)
                    sock.connect((self.local_host, self.local_port))
                return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.2)
        raise RemoteExecutionError(
            f"timed out waiting for ssh local forward on {self.local_host}:{self.local_port} ({last_error})"
        )

    def close(self) -> RemoteCompleted:
        """Kill the forward process group. Never returns rc=0."""
        if self._closed:
            rc = self._proc.poll()
            return RemoteCompleted(_rewrite_forward_exit(rc), "", self._stderr)
        self._closed = True
        rc = _stop_process_group(self._proc)
        stderr = self._consume_stderr()
        return RemoteCompleted(rc, "", stderr)

    def __enter__(self) -> LocalForward:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def open_local_forward(
    endpoint: Endpoint,
    remote_port: int,
    *,
    remote_host: str = "127.0.0.1",
    local_host: str = "127.0.0.1",
    local_port: int | None = None,
    ready_timeout_s: float | None = 15.0,
) -> LocalForward:
    """Open a local→remote forward and return a :class:`LocalForward` handle.

    Built on :meth:`Endpoint.for_long_stream`: independent connection,
    keepalives, ``ExitOnForwardFailure=yes``, ``-N``. Refuses a multiplexed
    endpoint the same way :func:`run_stream` does. When ``local_port`` is
    omitted an ephemeral loopback port is chosen. When ``ready_timeout_s``
    is not ``None``, the local port must accept connections before this
    returns.
    """
    endpoint = _as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    remote_port = _validate_port(remote_port, field="remote_port")
    if local_port is None:
        chosen_port = _find_free_local_port(local_host)
    else:
        chosen_port = _validate_port(local_port, field="local_port")
    cmd = local_forward_ssh_command(
        endpoint,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    handle = LocalForward(
        proc=proc,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    if ready_timeout_s is not None:
        try:
            handle.wait_ready(ready_timeout_s)
        except Exception:
            handle.close()
            raise
    return handle


def interactive_ssh_command(
    endpoint: Endpoint,
    remote_command: Sequence[str] = (),
) -> list[str]:
    """Argv for a one-off interactive bootstrap SSH.

    ``BatchMode=no``, password/keyboard-interactive only, never multiplexed.
    Refuses a multiplexed endpoint. Does not accept extra ``-o`` strings.
    This is first-contact bootstrap, not a general PTY facility.
    """
    if _uses_shared_mux(endpoint):
        raise RemoteExecutionError(INTERACTIVE_MUX_REFUSAL)
    timeout_s = max(1, int(endpoint.connect_timeout_ms / 1000))
    cmd = [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={timeout_s}",
        *_independent_ssh_connection_options(),
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-l",
        endpoint.user,
        "-p",
        str(endpoint.port),
        "--",
        endpoint.host,
    ]
    cmd.extend(str(item) for item in remote_command)
    return cmd


def run_interactive(
    endpoint: Endpoint,
    remote_command: Sequence[str] | str = (),
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a one-off interactive SSH, inheriting the local TTY.

    Returns the ``ssh`` returncode. ``env`` is merged into the process
    environment for wrappers such as ``SSH_ASKPASS``; it cannot inject
    SSH ``-o`` options. Refuses a multiplexed endpoint.
    """
    argv: Sequence[str]
    if isinstance(remote_command, str):
        argv = (remote_command,)
    else:
        argv = tuple(str(item) for item in remote_command)
    cmd = interactive_ssh_command(endpoint, argv)
    full_env = None if env is None else {**os.environ, **dict(env)}
    try:
        proc = subprocess.run(cmd, env=full_env)
    except FileNotFoundError as exc:
        raise RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    return int(proc.returncode)
