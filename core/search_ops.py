from __future__ import annotations

import time
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from .result import make_result, utc_now_iso
from .ssh_transport import run_remote_python

REMOTE_SEARCH_PY = r'''
import fnmatch
import glob as glob_mod
import json
import os
import pathlib
import subprocess
import sys

payload = json.loads(sys.stdin.read())
op = payload["op"]
root = pathlib.Path(payload["root"]).resolve()
cwd = pathlib.Path(payload.get("cwd") or payload["root"])

def fail(status, error=None, **extra):
    data = {"status": status}
    if error:
        data["error"] = error
    data.update(extra)
    print(json.dumps(data, sort_keys=True))
    raise SystemExit(0)

def resolve_path(raw):
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve()
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    return p, resolved

if op == "glob":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.is_dir():
        fail("not_directory", f"RemoteGlob path is not a directory: {base}")
    pattern = payload.get("pattern") or "*"
    limit = int(payload.get("limit") or 100)
    matches = []
    for item in glob_mod.glob(pattern, root_dir=str(base), recursive=True):
        path = base / item
        try:
            st = path.lstat()
        except OSError:
            continue
        matches.append({"path": str(path), "relpath": item, "type": "directory" if path.is_dir() else "file", "mtime_ns": st.st_mtime_ns, "size": st.st_size})
    matches.sort(key=lambda row: row["mtime_ns"], reverse=True)
    print(json.dumps({"status": "ok", "matches": matches[:limit], "truncated": len(matches) > limit}, sort_keys=True))
    raise SystemExit(0)

if op == "grep":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.exists():
        fail("not_found", f"RemoteGrep path does not exist: {base}")
    pattern = payload.get("pattern")
    if not pattern:
        fail("pattern_required", "RemoteGrep requires pattern")
    limit = int(payload.get("limit") or 100)
    output_mode = payload.get("output_mode") or "files_with_matches"
    glob_pattern = payload.get("glob")
    type_name = payload.get("type")
    multiline = bool(payload.get("multiline", False))
    warnings = []
    rg = subprocess.run(["bash", "-lc", "command -v rg"], capture_output=True, text=True, check=False)
    if rg.returncode == 0 and rg.stdout.strip():
        cmd = [rg.stdout.strip(), "--color", "never"]
        if multiline:
            cmd.append("-U")
        if glob_pattern:
            cmd.extend(["--glob", glob_pattern])
        if type_name:
            cmd.extend(["--type", type_name])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        else:
            cmd.append("-n")
        cmd.extend([pattern, str(base)])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode not in (0, 1):
            fail("failed", proc.stderr[-4000:])
        lines = proc.stdout.splitlines()
        print(json.dumps({
            "status": "ok",
            "engine": "rg",
            "output_mode": output_mode,
            "matches": lines[:limit],
            "truncated": len(lines) > limit,
            "warnings": warnings,
        }, sort_keys=True))
        raise SystemExit(0)

    warnings.append("rg not found; used Python fallback")
    matches = []
    paths = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    for path in paths:
        rel = str(path.relative_to(base)) if base.is_dir() else path.name
        if glob_pattern and not fnmatch.fnmatch(rel, glob_pattern):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern not in text:
            continue
        if output_mode == "files_with_matches":
            matches.append(str(path))
        elif output_mode == "count":
            matches.append(f"{path}:{text.count(pattern)}")
        else:
            for idx, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append(f"{path}:{idx}:{line}")
                    if len(matches) >= limit:
                        break
        if len(matches) >= limit:
            break
    print(json.dumps({"status": "ok", "engine": "python", "output_mode": output_mode, "matches": matches[:limit], "truncated": len(matches) >= limit, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

fail("unsupported_op", f"unsupported search op: {op}")
'''


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def remote_glob(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    limit: int = 100,
    respect_gitignore: bool = False,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.glob", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
            "op": "glob",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "path": base,
            "pattern": pattern,
            "limit": limit,
            "respect_gitignore": respect_gitignore,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    result = make_result(
        tool="remote.glob",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGlob found {len(matches)} paths.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": matches, "truncated": bool(data.get("truncated", False))},
        warnings=["respect_gitignore is not implemented for RemoteGlob"] if respect_gitignore else [],
        extra={"matches": matches, "truncated": bool(data.get("truncated", False)), "error": data.get("error")},
    )
    text = "\n".join([str(item.get("path", item)) for item in matches]) + ("\n<truncated>\n" if data.get("truncated") else "\n")
    return {"text": text, "result": result}


def remote_grep(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: str = "files_with_matches",
    multiline: bool = False,
    limit: int = 100,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.grep", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
            "op": "grep",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "path": base,
            "pattern": pattern,
            "glob": glob,
            "type": type,
            "output_mode": output_mode,
            "multiline": multiline,
            "limit": limit,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = data.get("warnings", []) if isinstance(data.get("warnings"), list) else []
    result = make_result(
        tool="remote.grep",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGrep found {len(matches)} matches.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": matches, "truncated": bool(data.get("truncated", False))},
        warnings=warnings,
        extra={"matches": matches, "engine": data.get("engine"), "output_mode": output_mode, "truncated": bool(data.get("truncated", False)), "error": data.get("error")},
    )
    text = "\n".join(str(item) for item in matches) + ("\n<truncated>\n" if data.get("truncated") else "\n")
    return {"text": text, "result": result}


def _path_blocked_result(endpoint: Endpoint, tool: str, path: str, error: str, started: str, start: float) -> dict[str, Any]:
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome="blocked",
        status="path_outside_root",
        summary=f"{tool} blocked for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"stderr": error},
        extra={"error": error},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}
