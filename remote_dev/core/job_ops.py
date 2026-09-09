from __future__ import annotations

import re
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
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


def _load_record(endpoint: Endpoint | None, job_id: str) -> tuple[Endpoint, dict[str, Any]]:
    job_id = require_job_id(job_id)
    if endpoint is not None:
        path = job_record_path(endpoint, job_id)
        if not path.exists():
            raise FileNotFoundError(f"unknown remote job id for endpoint: {job_id}")
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return endpoint, data
    found = find_job_record(job_id)
    if not found:
        raise FileNotFoundError(f"unknown remote job id: {job_id}")
    _, data = found
    return _endpoint_from_record(data), data


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
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    env = env or {}
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    job_id = require_job_id(job_id or new_job_id())
    cwd = cwd or endpoint.effective_cwd
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
        "authorization": authorization,
    }
    atomic_write_json(local_record, record)
    job_state = str(status_row.get("state") or "running")
    result = make_result(
        tool="remote.bash",
        target=endpoint.to_result_target(),
        outcome="success",
        status=job_state,
        summary="Remote background task started.",
        started_at=started,
        duration_ms=_duration_ms(start),
        refs={"job_record": str(local_record)},
        extra={
            "job": {
                "job_id": job_id,
                "status_tool": "remote.job_status",
                "tail_tool": "remote.job_tail",
                "stop_tool": "remote.job_stop",
                "remote_dir": remote_dir,
                "state": job_state,
                "quiet": status_row.get("quiet"),
                "receipt": status_row.get("receipt"),
            }
        },
    )
    text = f"RemoteBash started on {endpoint.user}@{endpoint.host}:{endpoint.port}\njob_id: {job_id}\nremote_dir: {remote_dir}\n"
    return {"text": text, "result": result}


def remote_job_status(endpoint: Endpoint | None, *, job_id: str) -> dict[str, Any]:
    endpoint, record = _load_record(endpoint, job_id)
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
    endpoint, record = _load_record(endpoint, job_id)
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


def remote_job_stop(endpoint: Endpoint | None, *, job_id: str, force: bool = False) -> dict[str, Any]:
    endpoint, record = _load_record(endpoint, job_id)
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
