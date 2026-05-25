from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .endpoint import Endpoint, substrate_root
from .path_policy import path_fingerprint
from .result import dumps, new_invocation_id, utc_now_iso


def state_root() -> Path:
    return substrate_root() / "state"


def endpoint_state_dir(endpoint: Endpoint) -> Path:
    return state_root() / "endpoints" / endpoint.endpoint_id


def ensure_endpoint_state(endpoint: Endpoint) -> Path:
    base = endpoint_state_dir(endpoint)
    for name in ("context", "reads", "logs", "jobs", "artifacts", "patches"):
        (base / name).mkdir(parents=True, exist_ok=True)
    endpoint_path = base / "endpoint.json"
    if not endpoint_path.exists():
        atomic_write_json(
            endpoint_path,
            {
                "schema_version": "remote-dev.endpoint.v1",
                "endpoint_id": endpoint.endpoint_id,
                "host": endpoint.host,
                "port": endpoint.port,
                "user": endpoint.user,
                "root": endpoint.root,
                "cwd": endpoint.effective_cwd,
                "kind": endpoint.kind,
                "alias": endpoint.alias,
                "created_at": utc_now_iso(),
            },
        )
    return base


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(dumps(data) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def new_log_dir(endpoint: Endpoint, tool_kind: str, invocation_id: str | None = None) -> Path:
    base = ensure_endpoint_state(endpoint)
    token = invocation_id or new_invocation_id()
    path = base / "logs" / tool_kind / token
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_ledger_path(endpoint: Endpoint, file_path: str) -> Path:
    return ensure_endpoint_state(endpoint) / "reads" / f"{path_fingerprint(file_path)}.json"


def write_read_ledger(endpoint: Endpoint, file_info: dict[str, Any]) -> Path:
    path = read_ledger_path(endpoint, str(file_info["path"]))
    payload = {
        "schema_version": "remote-dev.read_ledger.v1",
        "endpoint_id": endpoint.endpoint_id,
        "file_path": file_info["path"],
        "root": endpoint.root,
        "sha256": file_info["sha256"],
        "size": file_info["size"],
        "mtime_ns": file_info["mtime_ns"],
        "read_at": utc_now_iso(),
        "offset": file_info.get("offset"),
        "limit": file_info.get("limit"),
        "conversation_id": file_info.get("conversation_id"),
    }
    atomic_write_json(path, payload)
    return path


def load_read_ledger(endpoint: Endpoint, file_path: str) -> dict[str, Any] | None:
    path = read_ledger_path(endpoint, file_path)
    if not path.exists():
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None


def job_record_path(endpoint: Endpoint, job_id: str) -> Path:
    return ensure_endpoint_state(endpoint) / "jobs" / f"{job_id}.json"


def find_job_record(job_id: str) -> tuple[Path, dict[str, Any]] | None:
    root = state_root() / "endpoints"
    if not root.exists():
        return None
    for path in root.glob(f"*/jobs/{job_id}.json"):
        data = read_json(path)
        if isinstance(data, dict):
            return path, data
    return None


def list_endpoint_records() -> list[dict[str, Any]]:
    root = state_root() / "endpoints"
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/endpoint.json")):
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append({**data, "state_dir": str(path.parent)})
    return records


def latest_context_path(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "context" / "latest.json"


def jobs_dir(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "jobs"


def artifacts_dir(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "artifacts"


def list_job_records(endpoint_id: str) -> list[dict[str, Any]]:
    directory = jobs_dir(endpoint_id)
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append({**data, "local_record": str(path)})
    return records


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
