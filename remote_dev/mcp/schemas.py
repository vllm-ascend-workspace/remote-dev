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
    "remote.read": schema({"file_path": {"type": "string"}, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 200, "maximum": 500}, "client_context_id": {"type": "string"}}, ["file_path"]),
    "remote.write": schema({"file_path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}, "create_dirs": {"type": "boolean"}, "client_context_id": {"type": "string"}}, ["file_path", "content"]),
    "remote.edit": schema({"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}, "client_context_id": {"type": "string"}}, ["file_path", "old_string", "new_string"]),
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
            "client_context_id": {"type": "string"},
        },
        ["file_path", "edits"],
    ),
    "remote.bash": schema({"command": {"type": "string"}, "description": {"type": "string"}, "timeout_ms": {"type": "integer"}, "timeout": {"type": "integer"}, "run_in_background": {"type": "boolean"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
    "remote.glob": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}, "respect_gitignore": {"type": "boolean", "description": "Omit paths ignored by .gitignore. Uses git check-ignore when the remote tree is a git worktree; otherwise applies .gitignore rules in-process."}}, ["pattern"]),
    "remote.grep": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "type": {"type": "string"}, "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"]}, "multiline": {"type": "boolean"}, "limit": {"type": "integer", "maximum": 500}}, ["pattern"]),
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
    "remote.artifact_manifest": schema({"remote_path": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_pull": schema({"remote_path": {"type": "string"}, "local_dir": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_push": schema({"local_path": {"type": "string"}, "remote_path": {"type": "string"}}, ["local_path", "remote_path"]),
    "remote.context_snapshot": schema({"live_probe": {"type": "boolean", "default": True}}),
    "remote.probe": schema({}),
}

ALIASES: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}
