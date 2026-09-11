from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from remote_dev import package_version
from remote_dev.core.artifact_ops import remote_artifact_manifest, remote_artifact_pull, remote_artifact_push
from remote_dev.core.context_snapshot import remote_context_snapshot, remote_probe
from remote_dev.core.endpoint import EndpointError, has_selector, resolve_endpoint
from remote_dev.core.file_ops import remote_edit, remote_ls, remote_multi_edit, remote_read, remote_write
from remote_dev.core.job_ops import remote_job_stdin, remote_job_status, remote_job_stop, remote_job_tail
from remote_dev.core.patch_ops import remote_apply_patch
from remote_dev.core.search_ops import remote_glob, remote_grep
from remote_dev.core.shell_ops import remote_bash
from remote_dev.mcp.schemas import TOOL_SCHEMAS, normalize_arguments
from remote_dev.result import make_result

TOOL_NAMES = tuple(name.removeprefix("remote.") for name in TOOL_SCHEMAS)


def configure_cli_streams() -> None:
    """The JSON CLI protocol uses UTF-8, including redirected Windows pipes."""
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def add_endpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--cwd", "--workdir", dest="cwd", default=None, help="Working directory (--workdir is the Codex exec_command habit).")
    parser.add_argument("--runtime-env", dest="runtime_env", action="store_true", default=None)
    parser.add_argument("--no-runtime-env", dest="runtime_env", action="store_false")
    parser.add_argument("--runtime-env-file", dest="runtime_env_file", help="Remote profile script sourced before commands when runtime env is enabled.")
    parser.add_argument("--identity-file")
    parser.add_argument("--connect-timeout-ms", type=int)
    parser.add_argument("--alias", help="Endpoint alias from the endpoint alias files.")
    parser.add_argument(
        "--ssh-mux",
        dest="ssh_mux",
        action="store_true",
        default=None,
        help="Use the shared ControlMaster for this endpoint (overrides REMOTE_DEV_SSH_MUX).",
    )
    parser.add_argument(
        "--no-ssh-mux",
        dest="ssh_mux",
        action="store_false",
        help=(
            "Force an independent SSH connection for this endpoint "
            "(ControlMaster=no, ControlPath=none, ControlPersist=no). "
            "Required for long-lived tunnels and hour-scale streams."
        ),
    )
    parser.add_argument(
        "--keepalive",
        dest="keepalive",
        action="store_true",
        default=None,
        help="Add ServerAliveInterval/CountMax. Mechanism flag; hour-scale streams should use --long-stream.",
    )
    parser.add_argument(
        "--long-stream",
        dest="long_stream",
        action="store_true",
        default=None,
        help=(
            "Independent SSH connection plus keepalive "
            "(same as Endpoint.for_long_stream). Cannot be combined with --ssh-mux."
        ),
    )
    parser.add_argument(
        "--selector",
        action="append",
        metavar="KEY=VALUE",
        help="Extra selector field for a registered endpoint resolver (repeatable), e.g. --selector lab=gpu-1.",
    )


def parse_selectors(items: list[str] | None) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"bad --selector item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"bad --selector item {item!r}; empty key")
        selectors[key] = value
    return selectors


def endpoint_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "host",
        "port",
        "user",
        "root",
        "cwd",
        "runtime_env",
        "runtime_env_file",
        "identity_file",
        "connect_timeout_ms",
        "alias",
        "ssh_mux",
        "keepalive",
    ):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    if getattr(args, "long_stream", None):
        if payload.get("ssh_mux") is True:
            raise ValueError(
                "--long-stream cannot be combined with --ssh-mux: ControlMaster "
                "delegates -N forwards to the mux master and the client exits "
                "rc=0 immediately, tearing the tunnel down. OpenSSH "
                "first-option-wins makes a later ControlMaster=no override "
                "ineffective. Use --long-stream alone (or Endpoint.for_long_stream)."
            )
        payload["ssh_mux"] = False
        payload["keepalive"] = True
    payload.update(parse_selectors(getattr(args, "selector", None)))
    return payload


def parse_env(items: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"bad --env item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def print_payload(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("result", {}).get("outcome") in {"success", "cancelled"} else 1


def error_payload(tool: str, *, outcome: str, status: str, error: str) -> dict[str, Any]:
    result = make_result(
        tool=f"remote.{tool}",
        target={"kind": "unresolved"},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"remote.{tool} {status}.",
        preview={"stderr": error[-4000:]},
        extra={"error": error},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def build_parser(tool: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"remote-dev {tool.replace('_', '-')}")
    add_endpoint_args(parser)
    parser.add_argument("--input-json", help="Read complete tool arguments from a JSON file, or '-' for stdin.")
    parser.add_argument("--timeout-ms", type=int, default=120000)
    parser.add_argument("--client-context-id")
    if tool == "bash":
        parser.add_argument("--command", "--cmd", dest="command", required=False, help="Shell command (--cmd is the Codex exec_command habit).")
        parser.add_argument("--description")
        parser.add_argument("--run-in-background", action="store_true")
        parser.add_argument("--interactive", action="store_true", help="Background jobs only: keep stdin writable via remote-dev job-stdin.")
        parser.add_argument("--yield-time-ms", type=int, default=None, help="Background jobs only: poll up to this long before returning, then include the current output tail.")
        parser.add_argument("--max-output-tokens", type=int, default=None, help="Cap returned output, 4 characters per token, per stream.")
        parser.add_argument("--tty", action="store_true", help="NOT supported (no PTY); passing it returns an explicit capability error.")
        parser.add_argument("--env", action="append")
    elif tool == "read":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--offset", type=int, default=1)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--allow-symlink", action="store_true")
    elif tool == "ls":
        parser.add_argument("--path")
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--all", action="store_true")
    elif tool == "write":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--content")
        parser.add_argument("--content-file")
        parser.add_argument("--overwrite", action="store_true")
        parser.add_argument("--append", action="store_true", help="Append content at end of file instead of replacing it (mutually exclusive with --overwrite).")
        parser.add_argument("--create-dirs", action="store_true")
    elif tool == "edit":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--old-string")
        parser.add_argument("--new-string")
        parser.add_argument("--replace-all", action="store_true")
    elif tool == "multi_edit":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--edits-json")
    elif tool == "glob":
        parser.add_argument("--pattern", required=False)
        parser.add_argument("--path")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--respect-gitignore",
            action="store_true",
            help="Omit paths ignored by .gitignore (git check-ignore, or in-process rules).",
        )
    elif tool == "grep":
        parser.add_argument("--pattern", required=False)
        parser.add_argument("--path")
        parser.add_argument("--glob")
        parser.add_argument("--type")
        parser.add_argument("--output-mode", default="files_with_matches", choices=["files_with_matches", "content", "count", "count_matches"])
        parser.add_argument("--multiline", action="store_true")
        parser.add_argument("--ignore-case", dest="case_insensitive", action="store_true")
        parser.add_argument("--context-lines", type=int, default=0)
        parser.add_argument("--before-context", type=int, default=0)
        parser.add_argument("--after-context", type=int, default=0)
        parser.add_argument("--no-line-numbers", dest="line_numbers", action="store_false", default=None)
        parser.add_argument("--include-ignored", action="store_true", help="Also search hidden and .gitignore-d paths (grep fallback approximates; see docs).")
        parser.add_argument("--offset", type=int, default=0)
        parser.add_argument("--head-limit", type=int, default=None, help="Alias of --limit.")
        parser.add_argument("--limit", type=int, default=100)
    elif tool == "apply_patch":
        parser.add_argument("--patch")
        parser.add_argument("--patch-file")
        parser.add_argument("--command")
    elif tool in {"job_status", "job_tail", "job_stop", "job_stdin"}:
        parser.add_argument("--job-id", required=False)
        if tool == "job_tail":
            parser.add_argument("--lines", type=int, default=80)
            parser.add_argument("--stream", default="both", choices=["stdout", "stderr", "both"])
        if tool == "job_stdin":
            parser.add_argument("--chars", help="Bytes to write to the job's stdin; omit to only poll new output.")
            parser.add_argument("--eof", action="store_true", help="Close the job's stdin after writing.")
            parser.add_argument("--yield-time-ms", type=int, default=None, help="Poll up to this long before returning new output.")
            parser.add_argument("--max-output-tokens", type=int, default=None, help="Cap returned new output, 4 characters per token, per stream.")
        if tool == "job_stop":
            parser.add_argument("--force", action="store_true")
    elif tool in {"artifact_manifest", "artifact_pull", "artifact_push"}:
        parser.add_argument("--remote-path", required=False)
        if tool == "artifact_pull":
            parser.add_argument("--local-dir")
        if tool == "artifact_push":
            parser.add_argument("--local-path")
    elif tool == "monitor":
        parser.add_argument("--command", required=False)
        parser.add_argument("--description")
        parser.add_argument("--pattern")
        parser.add_argument("--env", action="append")
    elif tool == "context_snapshot":
        parser.add_argument("--no-live-probe", action="store_true")
    elif tool == "probe":
        parser.add_argument("--diagnose-connection", action="store_true")
    return parser


def load_input_json(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--input-json must be a JSON object")
    return data


def run_tool(tool: str, args: argparse.Namespace) -> dict[str, Any]:
    data = load_input_json(args.input_json) if args.input_json else {}
    data = {**endpoint_payload(args), **data}
    # One shared alias layer: --input-json accepts the same client-native
    # parameter names as the MCP dispatcher (path, line_offset, -i, ...).
    data = normalize_arguments(f"remote.{tool}", data)
    endpoint = None
    if tool not in {"job_status", "job_tail", "job_stop", "job_stdin"} or has_selector(data):
        endpoint = resolve_endpoint(data)
    timeout_ms = int(data.get("timeout_ms") or args.timeout_ms)
    if tool == "bash":
        assert endpoint is not None
        command = data.get("command") or args.command
        if not command:
            raise ValueError("remote.bash requires command (alias: cmd)")
        return remote_bash(
            endpoint,
            command=command,
            cwd=data.get("cwd"),
            description=data.get("description") or args.description,
            timeout_ms=timeout_ms,
            run_in_background=bool(data.get("run_in_background", args.run_in_background)),
            runtime_env=data.get("runtime_env"),
            env=data.get("env") if isinstance(data.get("env"), dict) else parse_env(args.env),
            interactive=bool(data.get("interactive", args.interactive)),
            yield_time_ms=(data.get("yield_time_ms") if data.get("yield_time_ms") is not None else args.yield_time_ms),
            max_output_tokens=(data.get("max_output_tokens") if data.get("max_output_tokens") is not None else args.max_output_tokens),
            tty=bool(data.get("tty", args.tty)),
        )
    if tool == "monitor":
        assert endpoint is not None
        monitor_command = data.get("command") or args.command
        if not monitor_command:
            raise ValueError("remote.monitor requires command (alias: cmd)")
        return remote_bash(endpoint, command=monitor_command, cwd=data.get("cwd"), description=data.get("description") or args.description, timeout_ms=timeout_ms, run_in_background=True, runtime_env=data.get("runtime_env"), env=data.get("env") if isinstance(data.get("env"), dict) else parse_env(args.env))
    if tool == "read":
        assert endpoint is not None
        file_path = data.get("file_path") or args.file_path
        if not file_path:
            raise ValueError("remote.read requires file_path (alias: path)")
        return remote_read(endpoint, file_path=file_path, offset=int(data.get("offset", args.offset)), limit=int(data.get("limit", args.limit)), allow_symlink=bool(data.get("allow_symlink", args.allow_symlink)), client_context_id=data.get("client_context_id") or args.client_context_id, timeout_ms=timeout_ms)
    if tool == "ls":
        assert endpoint is not None
        return remote_ls(endpoint, path=data.get("path") or args.path, limit=int(data.get("limit", args.limit)), all=bool(data.get("all", args.all)), timeout_ms=timeout_ms)
    if tool == "write":
        assert endpoint is not None
        content = data.get("content")
        if content is None and args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        file_path = data.get("file_path") or args.file_path
        if not file_path:
            raise ValueError("remote.write requires file_path (alias: path)")
        return remote_write(endpoint, file_path=file_path, content=str(content or ""), overwrite=bool(data.get("overwrite", args.overwrite)), append=bool(data.get("append", args.append)), create_dirs=bool(data.get("create_dirs", args.create_dirs)), client_context_id=data.get("client_context_id") or args.client_context_id, timeout_ms=timeout_ms)
    if tool == "edit":
        assert endpoint is not None
        file_path = data.get("file_path") or args.file_path
        if not file_path:
            raise ValueError("remote.edit requires file_path (alias: path)")
        return remote_edit(endpoint, file_path=file_path, old_string=data.get("old_string") if data.get("old_string") is not None else args.old_string, new_string=data.get("new_string") if data.get("new_string") is not None else args.new_string, replace_all=bool(data.get("replace_all", args.replace_all)), client_context_id=data.get("client_context_id") or args.client_context_id, timeout_ms=timeout_ms)
    if tool == "multi_edit":
        assert endpoint is not None
        edits = data.get("edits")
        if edits is None and args.edits_json:
            edits = json.loads(args.edits_json)
        file_path = data.get("file_path") or args.file_path
        if not file_path:
            raise ValueError("remote.multi_edit requires file_path (alias: path)")
        return remote_multi_edit(endpoint, file_path=file_path, edits=edits or [], client_context_id=data.get("client_context_id") or args.client_context_id, timeout_ms=timeout_ms)
    if tool == "glob":
        assert endpoint is not None
        return remote_glob(endpoint, pattern=data.get("pattern") or args.pattern or "*", path=data.get("path") or args.path, limit=int(data.get("limit", args.limit)), respect_gitignore=bool(data.get("respect_gitignore", args.respect_gitignore)), timeout_ms=timeout_ms)
    if tool == "grep":
        assert endpoint is not None
        args_limit = args.head_limit if args.head_limit is not None else args.limit
        return remote_grep(endpoint, pattern=data.get("pattern") or args.pattern or "", path=data.get("path") or args.path, glob=data.get("glob") or args.glob, type=data.get("type") or args.type, output_mode=data.get("output_mode") or args.output_mode, multiline=bool(data.get("multiline", args.multiline)), case_insensitive=bool(data.get("case_insensitive", args.case_insensitive)), before_context=int(data.get("before_context") or args.before_context), after_context=int(data.get("after_context") or args.after_context), context_lines=int(data.get("context_lines") or args.context_lines), line_numbers=data.get("line_numbers", args.line_numbers), include_ignored=bool(data.get("include_ignored", args.include_ignored)), offset=int(data.get("offset") or args.offset), limit=int(data.get("limit", args_limit)), timeout_ms=timeout_ms)
    if tool == "apply_patch":
        assert endpoint is not None
        patch = data.get("patch") or args.patch
        if patch is None and args.patch_file:
            patch = Path(args.patch_file).read_text(encoding="utf-8")
        return remote_apply_patch(endpoint, patch=patch, command=data.get("command") or args.command, cwd=data.get("cwd"), timeout_ms=timeout_ms)
    if tool == "job_status":
        return remote_job_status(endpoint, job_id=data.get("job_id") or args.job_id)
    if tool == "job_tail":
        return remote_job_tail(endpoint, job_id=data.get("job_id") or args.job_id, lines=int(data.get("lines", args.lines)), stream=data.get("stream") or args.stream)
    if tool == "job_stop":
        return remote_job_stop(endpoint, job_id=data.get("job_id") or args.job_id, force=bool(data.get("force", args.force)))
    if tool == "job_stdin":
        return remote_job_stdin(
            endpoint,
            job_id=data.get("job_id") or args.job_id,
            chars=data.get("chars") if data.get("chars") is not None else args.chars,
            eof=bool(data.get("eof", args.eof)),
            yield_time_ms=(data.get("yield_time_ms") if data.get("yield_time_ms") is not None else args.yield_time_ms),
            max_output_tokens=(data.get("max_output_tokens") if data.get("max_output_tokens") is not None else args.max_output_tokens),
        )
    if tool == "artifact_manifest":
        assert endpoint is not None
        return remote_artifact_manifest(endpoint, remote_path=data.get("remote_path") or args.remote_path, timeout_ms=timeout_ms)
    if tool == "artifact_pull":
        assert endpoint is not None
        return remote_artifact_pull(endpoint, remote_path=data.get("remote_path") or args.remote_path, local_dir=data.get("local_dir") or args.local_dir, timeout_ms=timeout_ms)
    if tool == "artifact_push":
        assert endpoint is not None
        return remote_artifact_push(endpoint, local_path=data.get("local_path") or args.local_path, remote_path=data.get("remote_path") or args.remote_path, timeout_ms=timeout_ms)
    if tool == "context_snapshot":
        assert endpoint is not None
        return remote_context_snapshot(endpoint, timeout_ms=timeout_ms, live_probe=not bool(data.get("no_live_probe", args.no_live_probe)))
    if tool == "probe":
        assert endpoint is not None
        return remote_probe(endpoint, timeout_ms=timeout_ms, diagnose_connection=bool(data.get("diagnose_connection", args.diagnose_connection)))
    raise ValueError(f"unsupported tool: {tool}")


def run_tool_main(tool: str, argv: list[str] | None = None) -> int:
    configure_cli_streams()
    parser = build_parser(tool)
    args = parser.parse_args(argv)
    try:
        return print_payload(run_tool(tool, args))
    except EndpointError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="endpoint_required", error=str(exc)))
    except FileNotFoundError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="not_found", error=str(exc)))
    except ValueError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="invalid_input", error=str(exc)))
    except Exception as exc:  # noqa: BLE001
        return print_payload(error_payload(tool, outcome="failed", status="exception", error=f"{type(exc).__name__}: {exc}"))


def _status_main() -> int:
    from remote_dev.core.state_store import state_root

    payload = {
        "name": "vaws-remote-dev",
        "version": package_version(),
        "package": "remote_dev",
        "state_dir": str(state_root()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-dev",
        description="SSH-backed remote twins of local agent tools.",
    )
    parser.add_argument("--version", action="version", version=package_version())
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("server", help="Run the MCP server on stdio.")
    sub.add_parser("status", help="Print package version and local runtime facts.")
    sub.add_parser("validate", help="Run local contract gates and optional live endpoint checks.")
    for name in TOOL_NAMES:
        sub.add_parser(name.replace("_", "-"), help=f"Invoke remote.{name}.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_cli_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        _build_root_parser().print_help()
        return 0
    if argv[0] in {"-V", "--version"}:
        print(package_version())
        return 0
    command = argv[0]
    rest = argv[1:]
    if command == "server":
        from remote_dev.mcp.server import main as server_main

        return server_main()
    if command == "status":
        return _status_main()
    if command == "validate":
        from remote_dev.tools.validate_remote_dev_scaffold import main as validate_main

        return validate_main(rest)
    tool = command.replace("-", "_")
    if tool in TOOL_NAMES:
        return run_tool_main(tool, rest)
    _build_root_parser().print_help()
    print(f"\nunknown command: {command}", file=sys.stderr)
    return 2
