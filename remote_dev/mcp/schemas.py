from __future__ import annotations

from typing import Any


ENDPOINT_PROPS: dict[str, Any] = {
    "host": {"type": "string"},
    "port": {"type": "integer"},
    "user": {"type": "string", "default": "root"},
    "root": {"type": "string", "default": "/"},
    "cwd": {"type": "string"},
    "runtime_env": {"type": "boolean", "default": True},
    "identity_file": {"type": "string"},
    "connect_timeout_ms": {"type": "integer", "default": 10000},
    "runtime_env_file": {"type": "string", "description": "Remote profile script sourced before commands when runtime_env is true."},
    "alias": {"type": "string", "description": "Name from the endpoint alias files."},
    "ssh_mux": {
        "type": "boolean",
        "description": (
            "Per-endpoint OpenSSH multiplexing. true uses the shared ControlMaster; "
            "false forces an independent connection (ControlMaster=no, ControlPath=none, "
            "ControlPersist=no). When omitted, REMOTE_DEV_SSH_MUX is the process default "
            "on POSIX. Native Windows has no Client ControlMaster; omitted ssh_mux already "
            "selects independent connections, and ssh_mux=true is a capability error. "
            "Hour-scale streams and ssh -N -L tunnels use Endpoint.for_long_stream, "
            "which sets ssh_mux=false. ControlMaster delegates -N forwards to the mux "
            "master and the client exits rc=0 immediately, tearing the tunnel down. "
            "OpenSSH first-option-wins makes a later ControlMaster=no override ineffective."
        ),
    },
    "keepalive": {
        "type": "boolean",
        "default": False,
        "description": (
            "Add ServerAliveInterval/CountMax. Mechanism flag, orthogonal to mux: "
            "it only adds TCP probes. Hour-scale streams use Endpoint.for_long_stream "
            "rather than this flag alone. Conditional: attaching ServerAlive to a "
            "ControlMaster client becomes master TCP policy (first-option-wins)."
        ),
    },
}

# Consumer resolvers may accept additional selector keys (for example a
# session or machine name). Schemas keep additionalProperties open so those
# keys pass through; the server rejects payloads no resolver claims.
ENDPOINT_SELECTOR_DESCRIPTION = (
    "Provide at least one endpoint selector: host and port together, or an "
    "alias from the endpoint alias files. A consumer-registered resolver may "
    "claim additional keys; remote-dev does not interpret session, profile, "
    "or binding identifiers itself. The server validates the selector before "
    "connecting."
)


def schema(
    props: dict[str, Any],
    required: list[str] | None = None,
    *,
    endpoint_selector: bool = True,
    description: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {**ENDPOINT_PROPS, **props},
        "required": required or [],
    }
    # Model providers accept different JSON Schema subsets. Keep the wire
    # schema a plain object; conditional requirements remain enforced by
    # resolve_endpoint / the tool implementation, not just by the client.
    constraints = [ENDPOINT_SELECTOR_DESCRIPTION] if endpoint_selector else []
    if description:
        constraints.append(description)
    if constraints:
        payload["description"] = " ".join(constraints)
    return payload


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "remote.read": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor native Read habit)."},
            "offset": {"type": "integer", "default": 1, "description": "1-based first line. Negative values count back from the end of the file."},
            "limit": {"type": "integer", "default": 200, "maximum": 500},
            "line_offset": {"type": "integer", "description": "Alias of offset (Kimi native Read habit)."},
            "n_lines": {"type": "integer", "description": "Alias of limit (Kimi native Read habit)."},
            "client_context_id": {"type": "string"},
        },
        description="file_path is required; the alias path is also accepted (Kimi/Cursor habit). Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.write": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor native Write habit)."},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "append": {"type": "boolean", "description": "Append content at end of file instead of replacing it (Kimi Write mode=append). Mutually exclusive with overwrite; creates a missing file."},
            "create_dirs": {"type": "boolean"},
            "client_context_id": {"type": "string"},
        },
        ["content"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.edit": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path."},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "client_context_id": {"type": "string"},
        },
        ["old_string", "new_string"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.multi_edit": schema(
        {
            "file_path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string", "default": ""},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["old_string"],
                },
            },
            "path": {"type": "string", "description": "Alias of file_path."},
            "client_context_id": {"type": "string"},
        },
        ["edits"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.bash": schema(
        {
            "command": {"type": "string"},
            "cmd": {"type": "string", "description": "Alias of command (Codex exec_command habit)."},
            "workdir": {"type": "string", "description": "Alias of cwd (Codex exec_command habit)."},
            "description": {"type": "string"},
            "timeout_ms": {"type": "integer"},
            "timeout": {"type": "integer"},
            "run_in_background": {"type": "boolean"},
            "tty": {
                "type": "boolean",
                "description": (
                    "PTY allocation. NOT supported: remote sessions use pipes, and this "
                    "flag returns an explicit capability error instead of being ignored. "
                    "Control bytes such as \\x03 are delivered as bytes, not signals; "
                    "cancel with remote.job_stop."
                ),
            },
            "interactive": {
                "type": "boolean",
                "description": (
                    "Background only: keep the job's stdin open so later remote.job_stdin "
                    "calls can write to it (Codex exec_command/write_stdin habit). The job "
                    "runs on the same process supervisor as every background job; cancel it "
                    "with remote.job_stop."
                ),
            },
            "yield_time_ms": {
                "type": "integer",
                "description": (
                    "Background only: after starting, keep polling up to this many "
                    "milliseconds for output or completion before returning, then include "
                    "fresh stdout/stderr from the start up to the max_output_tokens budget "
                    "(Codex exec_command yield habit). Later remote.job_stdin polls continue "
                    "exactly where this yield stopped. 0 or omitted returns immediately "
                    "after the start handshake."
                ),
            },
            "max_output_tokens": {
                "type": "integer",
                "description": (
                    "Cap the output returned by this call, counted as 4 characters per "
                    "token (approximation of token budgets, applied per stream). Full "
                    "output remains available through refs/remote.job_tail."
                ),
            },
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        description="command is required; the alias cmd is also accepted (Codex habit). Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.glob": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}, "respect_gitignore": {"type": "boolean", "description": "Omit paths ignored by .gitignore. Uses git check-ignore when the remote tree is a git worktree; otherwise applies .gitignore rules in-process."}}, ["pattern"]),
    "remote.grep": schema(
        {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "type": {"type": "string"},
            "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count", "count_matches"], "description": "count = matching lines per file (Claude habit); count_matches = total matches per file (Kimi habit, rg --count-matches). They differ when one line holds several matches."},
            "multiline": {"type": "boolean"},
            "case_insensitive": {"type": "boolean", "description": "Case-insensitive matching (rg -i / grep -i). Alias: -i."},
            "-i": {"type": "boolean", "description": "Alias of case_insensitive (Claude Code / Kimi native Grep parameter)."},
            "context_lines": {"type": "integer", "description": "Show N lines before and after each match in content mode (rg/grep -C). Alias: -C."},
            "-C": {"type": "integer", "description": "Alias of context_lines."},
            "before_context": {"type": "integer", "description": "Show N lines before each match in content mode (rg/grep -B). Alias: -B."},
            "-B": {"type": "integer", "description": "Alias of before_context."},
            "after_context": {"type": "integer", "description": "Show N lines after each match in content mode (rg/grep -A). Alias: -A."},
            "-A": {"type": "integer", "description": "Alias of after_context."},
            "line_numbers": {"type": "boolean", "description": "Prefix content matches with line numbers. Default true. Alias: -n."},
            "-n": {"type": "boolean", "description": "Alias of line_numbers."},
            "offset": {"type": "integer", "description": "Skip the first N result lines before applying the limit (pagination)."},
            "head_limit": {"type": "integer", "description": "Alias of limit (Kimi/Claude native Grep habit)."},
            "include_ignored": {
                "type": "boolean",
                "description": (
                    "Also search hidden paths and paths ignored by .gitignore "
                    "(rg --no-ignore --hidden). The grep fallback cannot evaluate "
                    ".gitignore rules; there it only lifts the default hidden/.git "
                    "directory excludes and reports a warning."
                ),
            },
            "limit": {"type": "integer", "maximum": 500},
        },
        ["pattern"],
    ),
    "remote.ls": schema({"path": {"type": "string"}, "limit": {"type": "integer"}, "all": {"type": "boolean"}}),
    "remote.monitor": schema({"command": {"type": "string"}, "description": {"type": "string"}, "timeout_ms": {"type": "integer"}, "pattern": {"type": "string"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
    "remote.apply_patch": schema(
        {
            "patch": {"type": "string", "description": "Codex apply_patch payload or unified diff. Prefer this field."},
            "command": {"type": "string", "description": "Legacy alias for the patch payload, not a shell command."},
            "timeout_ms": {"type": "integer"},
        },
        description="Provide a non-empty patch or command. If both are provided, patch takes precedence. Missing patch content is rejected by the server.",
    ),
    "remote.job_status": schema({"job_id": {"type": "string"}}, ["job_id"], endpoint_selector=False),
    "remote.job_tail": schema({"job_id": {"type": "string"}, "lines": {"type": "integer", "maximum": 500}, "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]}}, ["job_id"], endpoint_selector=False),
    "remote.job_stop": schema({"job_id": {"type": "string"}, "force": {"type": "boolean"}}, ["job_id"], endpoint_selector=False),
    "remote.job_stdin": schema(
        {
            "job_id": {"type": "string"},
            "chars": {"type": "string", "description": "Bytes to write to the job's stdin (Codex write_stdin habit). Omit or pass an empty string to only poll output. When stdin_buffer_full is reported, resend only the unwritten remainder: slice the original string at the returned written_chars character count (a byte count cannot slice a Unicode string)."},
            "eof": {"type": "boolean", "description": "Close the job's stdin after writing chars. Programs waiting for end-of-input then finish. If the write was only partially accepted (stdin_buffer_full), the close is deferred: resend the unwritten remainder with eof=true."},
            "yield_time_ms": {"type": "integer", "description": "After writing, keep polling up to this many milliseconds for output or completion before returning new output."},
            "max_output_tokens": {"type": "integer", "description": "Cap the new output returned by this call, counted as 4 characters per token, applied per stream. Skipped bytes are not lost: the read cursor only advances past what was returned."},
            "lines": {"type": "integer", "maximum": 500, "description": "Tail lines per stream when this call falls back to snapshot output (first poll). Incremental reads are byte-based."},
        },
        ["job_id"],
        endpoint_selector=False,
        description=(
            "The job must have been started with remote.bash run_in_background=true and "
            "interactive=true; otherwise the call fails with an actionable error. "
            "Cancellation stays with remote.job_stop. The endpoint is rebuilt from the "
            "local job record unless a selector is supplied."
        ),
    ),
    "remote.artifact_manifest": schema({"remote_path": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_pull": schema({"remote_path": {"type": "string"}, "local_dir": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_push": schema({"local_path": {"type": "string"}, "remote_path": {"type": "string"}}, ["local_path", "remote_path"]),
    "remote.context_snapshot": schema({"live_probe": {"type": "boolean", "default": True}}),
    "remote.probe": schema({"diagnose_connection": {"type": "boolean", "default": False, "description": "Compare SSH connections with a fixed read-only probe; never replays a business command."}}),
}

ALIASES: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}

# Thin client-native parameter aliases folded into the canonical fields by
# normalize_arguments. One shared mapping serves the MCP dispatcher and the
# CLI --input-json path; there is no per-client execution fork. A canonical
# key always wins over its alias, and unknown keys pass through untouched so
# consumer resolver selectors keep working.
PARAM_ALIASES: dict[str, dict[str, str]] = {
    "remote.read": {"path": "file_path", "line_offset": "offset", "n_lines": "limit"},
    "remote.write": {"path": "file_path"},
    "remote.edit": {"path": "file_path"},
    "remote.multi_edit": {"path": "file_path"},
    "remote.bash": {"cmd": "command", "workdir": "cwd"},
    "remote.monitor": {"cmd": "command", "workdir": "cwd"},
    "remote.grep": {
        "-i": "case_insensitive",
        "-A": "after_context",
        "-B": "before_context",
        "-C": "context_lines",
        "-n": "line_numbers",
        "head_limit": "limit",
    },
}


def normalize_arguments(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Fold known client-native alias keys into their canonical fields."""
    mapping = PARAM_ALIASES.get(tool)
    if not mapping:
        return args
    normalized = dict(args)
    for alias, canonical in mapping.items():
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized[alias]
        normalized.pop(alias, None)
    return normalized
