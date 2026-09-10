from __future__ import annotations

import re
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from remote_dev.core.endpoint import DEFAULT_CWD, DEFAULT_ROOT, Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS, compact_text
from remote_dev.core.runtime_env import runtime_env_lines
from remote_dev.core.state_store import atomic_write_json, find_job_record, job_record_path
from remote_dev.processes import control
from remote_dev.result import make_result, utc_now_iso

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV_PREFIX = "REMOTE_DEV_JOB_"
STOP_DRAIN_SECONDS = 2.0
MAX_YIELD_MS = 30000
MAX_INCREMENTAL_READ_BYTES = 32768


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"job-{stamp}-{uuid.uuid4().hex[:8]}"


def require_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value):
        raise ValueError("job id must be 3-96 chars from A-Z a-z 0-9 _ . -")
    return value


def require_env_name(value: str) -> str:
    if not ENV_NAME_RE.fullmatch(value):
        raise ValueError(f"invalid environment variable name: {value!r}")
    if value.startswith(RESERVED_ENV_PREFIX):
        raise ValueError(f"reserved environment variable name: {value!r}")
    return value


def remote_job_dir(endpoint: Endpoint, job_id: str) -> str:
    require_job_id(job_id)
    return str(PurePosixPath(endpoint.root) / ".remote-dev" / "jobs" / job_id)


def _timeout_seconds(timeout_ms: int | None) -> int | None:
    if timeout_ms is None or timeout_ms <= 0:
        return None
    seconds = int(timeout_ms / 1000)
    if seconds < 1:
        seconds = 1
    if seconds > 86400:
        seconds = 86400
    return seconds


def _job_command(endpoint: Endpoint, command: str, runtime_enabled: bool) -> str:
    preamble = runtime_env_lines(endpoint, runtime_enabled)
    if not preamble:
        return command
    return "; ".join([*preamble, f"bash -c {shlex.quote(command)}"])


def _record_cwd(target: dict[str, Any]) -> str:
    return str(target.get("cwd") or DEFAULT_CWD or target.get("root") or DEFAULT_ROOT)


def _endpoint_from_record(record: dict[str, Any]) -> Endpoint:
    target = record.get("target", {})
    return Endpoint(
        host=str(target["host"]),
        port=int(target["port"]),
        user=str(target.get("user") or "root"),
        root=str(target.get("root") or DEFAULT_ROOT),
        cwd=_record_cwd(target),
        runtime_env=bool(target.get("runtime_env", True)),
        runtime_env_file=str(target["runtime_env_file"]) if target.get("runtime_env_file") else None,
        kind=str(target.get("kind") or "direct-endpoint"),
        alias=str(target["alias"]) if target.get("alias") else None,
    )


def endpoint_from_job_record(record: dict[str, Any]) -> Endpoint:
    return _endpoint_from_record(record)


def _load_record(endpoint: Endpoint | None, job_id: str) -> tuple[Endpoint, dict[str, Any], Path]:
    job_id = require_job_id(job_id)
    if endpoint is not None:
        path = job_record_path(endpoint, job_id)
        if not path.exists():
            raise FileNotFoundError(f"unknown remote job id for endpoint: {job_id}")
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return endpoint, data, path
    found = find_job_record(job_id)
    if not found:
        raise FileNotFoundError(f"unknown remote job id: {job_id}")
    path, data = found
    return _endpoint_from_record(data), data, path


def _start_failure(
    endpoint: Endpoint,
    *,
    cwd: str,
    started: str,
    start: float,
    job_id: str,
    outcome: str,
    status: str,
    summary: str,
    error: str,
) -> dict[str, Any]:
    result = make_result(
        tool="remote.bash",
        target={**endpoint.to_result_target(), "cwd": cwd},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=summary,
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"error": error[-4000:], "job_id": job_id},
    )
    return {"text": summary + "\n", "result": result}


def _classify_start_error(exc: BaseException) -> tuple[str, str, str]:
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, FileNotFoundError) or "no such file" in lowered or "cwd" in lowered and "not" in lowered and "exist" in lowered:
        return "failed", "cwd_not_found", "Remote background task failed because cwd does not exist."
    if isinstance(exc, NotADirectoryError) or "not a directory" in lowered:
        return "failed", "cwd_not_directory", "Remote background task failed because cwd is not a directory."
    if "escapes the runtime root" in lowered or "outside" in lowered:
        return "blocked", "cwd_outside_root", "Remote background task blocked because cwd is outside root."
    if "reused with different" in lowered:
        return "blocked", "job_id_exists", "Remote background task blocked because job_id already exists."
    if "not a verified waiting supervisor" in lowered:
        return "blocked", "job_id_exists", "Remote background task blocked because job_id already exists."
    return "failed", "job_start_failed", "Remote background task failed to start."


def start_remote_job(
    endpoint: Endpoint,
    *,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    runtime_env: bool | None = None,
    description: str | None = None,
    job_id: str | None = None,
    interactive: bool = False,
    yield_time_ms: int | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    env = env or {}
    warnings: list[str] = []
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    job_id = require_job_id(job_id or new_job_id())
    cwd = cwd or endpoint.effective_cwd
    yield_ms = int(yield_time_ms or 0)
    if yield_ms < 0:
        yield_ms = 0
    if yield_ms > MAX_YIELD_MS:
        warnings.append(f"yield_time_ms clamped from {yield_ms} to {MAX_YIELD_MS}")
        yield_ms = MAX_YIELD_MS
    local_record = job_record_path(endpoint, job_id)
    found_record = find_job_record(job_id)
    if local_record.exists() or found_record:
        result = make_result(
            tool="remote.bash",
            target={**endpoint.to_result_target(), "cwd": cwd},
            outcome="blocked",
            status="job_id_exists",
            summary=f"Remote background task blocked because job_id already exists: {job_id}.",
            started_at=started,
            duration_ms=_duration_ms(start),
            refs={"job_record": str(found_record[0]) if found_record else str(local_record)},
            extra={"job_id": job_id},
        )
        return {"text": result["summary"] + "\n", "result": result}
    spec = {
        "command": _job_command(endpoint, command, runtime_enabled),
        "cwd": cwd,
        "env": {require_env_name(key): str(value) for key, value in env.items()},
        "timeout_seconds": _timeout_seconds(timeout_ms),
        "interactive": bool(interactive),
    }
    try:
        prepared = control(endpoint, job_id, "prepare", spec=spec)
    except (RemoteExecutionError, ValueError, RuntimeError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        outcome, status, summary = _classify_start_error(exc)
        return _start_failure(
            endpoint,
            cwd=cwd,
            started=started,
            start=start,
            job_id=job_id,
            outcome=outcome,
            status=status,
            summary=summary,
            error=str(exc),
        )
    authorization = {"token": uuid.uuid4().hex, "job_id": job_id}
    try:
        if prepared.get("state") == "prepared" and not prepared.get("gate_open"):
            status_row = control(endpoint, job_id, "go", authorization=authorization)
        else:
            status_row = prepared
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        try:
            control(endpoint, job_id, "stop", force=True)
        except Exception:
            pass
        outcome, status, summary = _classify_start_error(exc)
        return _start_failure(
            endpoint,
            cwd=cwd,
            started=started,
            start=start,
            job_id=job_id,
            outcome=outcome,
            status=status,
            summary=summary,
            error=str(exc),
        )
    remote_dir = str(status_row.get("remote_dir") or remote_job_dir(endpoint, job_id))
    record = {
        "schema_version": "remote-dev.job.v1",
        "job_id": job_id,
        "description": description,
        "target": endpoint.to_result_target(),
        "command_preview": command[:500],
        "cwd": cwd,
        "env_keys": sorted(env),
        "runtime_env": runtime_enabled,
        "runtime_env_file": endpoint.runtime_env_file,
        "remote_dir": remote_dir,
        "started_at": started,
        "timeout_ms": timeout_ms,
        "interactive": bool(interactive),
        "authorization": authorization,
    }
    atomic_write_json(local_record, record)
    job_state = str(status_row.get("state") or "running")
    yield_info: dict[str, Any] | None = None
    if yield_ms > 0:
        # Codex exec_command habit: hold the response briefly so fast commands
        # finish (or first output appears) inside the same tool call. The job
        # stays on the supervisor either way; job_stdin/job_tail continue it.
        # A yield polling failure must not kill the already-running job.
        try:
            deadline = time.monotonic() + yield_ms / 1000
            while time.monotonic() < deadline:
                row = control(endpoint, job_id, "status")
                job_state = str(row.get("state") or job_state)
                if row.get("quiet"):
                    break
                time.sleep(0.1)
            budget = _output_budget_bytes(max_output_tokens)
            # Same incremental cursor path as job_stdin polls: the initial
            # yield returns the first bytes up to the budget (not just the
            # last lines) and persists the cursors where it stopped, so a
            # follow-up job_stdin poll neither replays this output nor drops
            # the earlier unreturned bytes.
            tail_row = control(endpoint, job_id, "tail", stdout_offset=0, stderr_offset=0, max_bytes=budget)
            record["stdin_cursors"] = {
                "stdout_offset": int(tail_row.get("stdout_offset") or 0),
                "stderr_offset": int(tail_row.get("stderr_offset") or 0),
            }
            atomic_write_json(local_record, record)
            yield_info = {
                "yield_time_ms": yield_ms,
                "state": job_state,
                "stdout": str(tail_row.get("stdout") or ""),
                "stderr": str(tail_row.get("stderr") or ""),
                "max_bytes_per_stream": budget,
                "stdout_bytes_remaining": int(tail_row.get("stdout_bytes_remaining") or 0),
                "stderr_bytes_remaining": int(tail_row.get("stderr_bytes_remaining") or 0),
            }
            for stream_name in ("stdout", "stderr"):
                remaining = int(tail_row.get(f"{stream_name}_bytes_remaining") or 0)
                if remaining:
                    warnings.append(
                        f"{stream_name}: {remaining} more byte(s) pending beyond the initial yield budget; "
                        "continue with remote.job_stdin (interactive) or remote.job_tail"
                    )
        except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
            warnings.append(f"yield polling failed; the job is still running under the supervisor: {str(exc)[-500:]}")
    result = make_result(
        tool="remote.bash",
        target=endpoint.to_result_target(),
        outcome="success",
        status=job_state,
        summary="Remote background task started.",
        started_at=started,
        duration_ms=_duration_ms(start),
        refs={"job_record": str(local_record)},
        warnings=warnings,
        extra={
            "job": {
                "job_id": job_id,
                "status_tool": "remote.job_status",
                "tail_tool": "remote.job_tail",
                "stop_tool": "remote.job_stop",
                "stdin_tool": "remote.job_stdin" if interactive else None,
                "interactive": bool(interactive),
                "remote_dir": remote_dir,
                "state": job_state,
                "quiet": status_row.get("quiet"),
                "receipt": status_row.get("receipt"),
                "yield": yield_info,
            }
        },
    )
    text = f"RemoteBash started on {endpoint.user}@{endpoint.host}:{endpoint.port}\njob_id: {job_id}\nremote_dir: {remote_dir}\n"
    if interactive:
        text += "interactive: true (write to stdin with remote.job_stdin; cancel with remote.job_stop)\n"
    if yield_info is not None:
        text += f"state after yield: {job_state}\n__STDOUT__\n{yield_info['stdout']}__STDERR__\n{yield_info['stderr']}"
    return {"text": text, "result": result}


def remote_job_status(endpoint: Endpoint | None, *, job_id: str) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "status")
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_status",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job {job_id} status failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job": {**record, "error": str(exc)[-4000:]}},
        )
        return {"text": result["summary"] + "\n", "result": result}
    status = str(supervisor.get("state") or "unknown")
    result = make_result(
        tool="remote.job_status",
        target=endpoint.to_result_target(),
        outcome="success",
        status=status,
        summary=f"Remote job {job_id} is {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"job": {**record, "remote_status": supervisor, "quiet": supervisor.get("quiet")}},
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}


def remote_job_tail(endpoint: Endpoint | None, *, job_id: str, lines: int = 80, stream: str = "both") -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    warnings = []
    if lines > MAX_JOB_TAIL_LINES:
        warnings.append(f"lines clamped from {lines} to {MAX_JOB_TAIL_LINES}")
        lines = MAX_JOB_TAIL_LINES
    if lines < 1:
        lines = 1
    try:
        supervisor = control(endpoint, job_id, "tail", lines=lines)
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_tail",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job tail for {job_id} failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job_id": job_id, "error": str(exc)[-4000:]},
        )
        return {"text": result["summary"] + "\n", "result": result}
    requested: list[str] = []
    sections: list[str] = []
    missing: list[str] = []
    for name in ("stdout", "stderr"):
        if stream not in {name, "both"}:
            continue
        requested.append(name)
        if name not in supervisor:
            missing.append(name)
            continue
        body = str(supervisor.get(name) or "")
        sections.append(f"__{name.upper()}__\n{body}".rstrip() + ("\n" if body else ""))
    for name in missing:
        warnings.append(f"{name}.log does not exist in the remote job dir")
    all_missing = bool(requested) and len(missing) == len(requested)
    text = compact_text("".join(sections), limit=MAX_TEXT_CHARS)
    failed = all_missing or supervisor.get("state") == "absent"
    result = make_result(
        tool="remote.job_tail",
        target=endpoint.to_result_target(),
        outcome="failed" if failed else "success",
        status="log_not_found" if all_missing else "ok",
        summary=f"Remote job tail for {job_id}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"tail": text, "stderr": ""},
        warnings=warnings,
        extra={"job_id": job_id, "lines": lines, "missing_logs": missing, "state": supervisor.get("state")},
    )
    return {"text": text, "result": result}


def _output_budget_bytes(max_output_tokens: int | None) -> int:
    """Approximate a token budget as bytes, 4 characters per token.

    Codex counts real model tokens; remote-dev cannot tokenize here, so the
    documented fixed ratio is applied per stream and never silently drops the
    remainder: the read cursor only advances past returned bytes.
    """
    if max_output_tokens is None:
        return MAX_INCREMENTAL_READ_BYTES
    return max(256, min(MAX_INCREMENTAL_READ_BYTES, int(max_output_tokens) * 4))


def remote_job_stdin(
    endpoint: Endpoint | None,
    *,
    job_id: str,
    chars: str | None = None,
    eof: bool = False,
    yield_time_ms: int | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Write bytes to a running interactive job's stdin, then report fresh output.

    Codex write_stdin habit: empty ``chars`` is a poll. Output is incremental:
    a per-stream byte cursor in the local job record advances past exactly the
    bytes this call returns, so repeated polls never replay earlier output and
    budget-capped calls lose nothing. ``eof`` closes the input so programs
    waiting for end-of-input finish. Cancellation stays with
    ``remote.job_stop``; lifecycle, logs, exit code and descendant drain are
    the shared supervisor's, exactly like every other background job.
    """
    endpoint, record, record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    warnings = []
    if not record.get("interactive"):
        error = (
            f"job {job_id} was not started with interactive=true and has no stdin channel; "
            "restart it with remote.bash run_in_background=true interactive=true"
        )
        result = make_result(
            tool="remote.job_stdin",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="not_interactive",
            summary=f"Remote job {job_id} has no stdin channel.",
            started_at=started,
            duration_ms=_duration_ms(start),
            preview={"stderr": error},
            extra={"job_id": job_id, "error": error},
        )
        return {"text": result["summary"] + "\n" + error + "\n", "result": result}
    yield_ms = max(0, int(yield_time_ms or 0))
    if yield_ms > MAX_YIELD_MS:
        warnings.append(f"yield_time_ms clamped from {yield_ms} to {MAX_YIELD_MS}")
        yield_ms = MAX_YIELD_MS
    try:
        reply = control(endpoint, job_id, "stdin", data=chars or "", eof=bool(eof))
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_stdin",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job {job_id} stdin write failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job_id": job_id, "error": str(exc)[-4000:]},
        )
        return {"text": result["summary"] + "\n", "result": result}
    if not reply.get("accepted"):
        reason = str(reply.get("reason") or "stdin write was not accepted")
        result = make_result(
            tool="remote.job_stdin",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="stdin_rejected",
            summary=f"Remote job {job_id} stdin write rejected.",
            started_at=started,
            duration_ms=_duration_ms(start),
            preview={"stderr": reason},
            extra={"job_id": job_id, "error": reason, "state": reply.get("state")},
        )
        return {"text": result["summary"] + "\n" + reason + "\n", "result": result}
    if reply.get("stdin_buffer_full"):
        warnings.append(
            f"remote stdin buffer is full; {reply.get('written', 0)} bytes "
            f"({reply.get('written_chars', 0)} chars) accepted — retry the exact unwritten "
            "remainder after the job drains it, slicing the original chars at written_chars "
            "(a byte count cannot slice a Unicode string)"
        )
    if reply.get("eof_deferred"):
        warnings.append(
            "eof was deferred: stdin closes only once every byte of this call is accepted; "
            "resend the unwritten remainder with eof=true"
        )
    state = str(reply.get("state") or "running")
    if yield_ms > 0:
        try:
            deadline = time.monotonic() + yield_ms / 1000
            while time.monotonic() < deadline:
                row = control(endpoint, job_id, "status")
                state = str(row.get("state") or state)
                if row.get("quiet"):
                    break
                time.sleep(0.1)
        except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
            warnings.append(f"yield polling failed: {str(exc)[-500:]}")
    cursors = record.get("stdin_cursors") if isinstance(record.get("stdin_cursors"), dict) else {}
    stdout_offset = max(0, int(cursors.get("stdout_offset") or 0))
    stderr_offset = max(0, int(cursors.get("stderr_offset") or 0))
    max_bytes = _output_budget_bytes(max_output_tokens)
    try:
        tail_row = control(
            endpoint,
            job_id,
            "tail",
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            max_bytes=max_bytes,
        )
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        tail_row = {}
        warnings.append(f"incremental read after stdin write failed: {str(exc)[-500:]}")
    state = str(tail_row.get("state") or state)
    exit_code = (tail_row.get("result") or {}).get("exit_code") if isinstance(tail_row.get("result"), dict) else None
    new_stdout = str(tail_row.get("stdout") or "")
    new_stderr = str(tail_row.get("stderr") or "")
    if tail_row:
        record["stdin_cursors"] = {
            "stdout_offset": int(tail_row.get("stdout_offset", stdout_offset)),
            "stderr_offset": int(tail_row.get("stderr_offset", stderr_offset)),
        }
        atomic_write_json(record_path, record)
    for stream_name in ("stdout", "stderr"):
        remaining = int(tail_row.get(f"{stream_name}_bytes_remaining") or 0)
        if remaining:
            warnings.append(
                f"{stream_name}: {remaining} more byte(s) pending beyond this call's "
                "output budget; call remote.job_stdin again to continue"
            )
    sections: list[str] = []
    for name, body in (("STDOUT", new_stdout), ("STDERR", new_stderr)):
        sections.append(f"__{name}__\n{body}".rstrip() + ("\n" if body else ""))
    text = compact_text("".join(sections), limit=MAX_TEXT_CHARS)
    header = f"Remote job {job_id}: wrote {reply.get('written', 0)} bytes" + (", stdin closed (eof)" if reply.get("eof") else "") + f"; state {state}"
    if exit_code is not None:
        header += f"; exit code {exit_code}"
    result = make_result(
        tool="remote.job_stdin",
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=f"Remote job {job_id} stdin updated.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"tail": text, "stderr": ""},
        warnings=warnings,
        extra={
            "job_id": job_id,
            "stdin": reply,
            "state": state,
            "exit_code": exit_code,
            "eof": bool(eof),
            "new_output": {"stdout": new_stdout, "stderr": new_stderr},
            "cursors": record.get("stdin_cursors", {}),
            "max_bytes_per_stream": max_bytes,
        },
    )
    return {"text": header + "\n" + text, "result": result}


def remote_job_stop(endpoint: Endpoint | None, *, job_id: str, force: bool = False) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "stop", force=force)
        deadline = time.monotonic() + STOP_DRAIN_SECONDS
        while not supervisor.get("quiet") and time.monotonic() < deadline:
            time.sleep(0.05)
            supervisor = control(endpoint, job_id, "status")
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_stop",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job {job_id} stop failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job_id": job_id, "error": str(exc)[-4000:]},
        )
        return {"text": result["summary"] + "\n", "result": result}
    state = str(supervisor.get("state") or "unknown")
    quiet = bool(supervisor.get("quiet"))
    if quiet and state in {"cancelled", "succeeded", "failed", "timeout"}:
        outcome = "cancelled" if state == "cancelled" else "success"
        status = state
    elif quiet:
        outcome, status = "success", state
    else:
        outcome, status = "failed", state
    result = make_result(
        tool="remote.job_stop",
        target=endpoint.to_result_target(),
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"Remote job {job_id} {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"job_id": job_id, "quiet": quiet, "remote_status": supervisor},
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}
