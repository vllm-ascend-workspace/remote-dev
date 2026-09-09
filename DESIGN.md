# Remote Developer Substrate Design

remote-dev makes remote development feel like local development for Codex,
Claude Code, Cursor, Kimi, Grok and other MCP-capable agents. Local work keeps
using the client's native Read/Edit/Write/Bash/Glob/Grep/apply_patch tools;
remote work uses the matching `remote.*` tool and only adds endpoint fields.

## Architecture

Layer A is the remote-native developer tool surface:

- RemoteRead / `remote.read`
- RemoteWrite / `remote.write`
- RemoteEdit / `remote.edit`
- RemoteMultiEdit / `remote.multi_edit`
- RemoteBash / `remote.bash`
- RemoteGlob / `remote.glob`
- RemoteGrep / `remote.grep`
- RemoteLS / `remote.ls`
- RemoteMonitor / `remote.monitor`
- RemoteApplyPatch / `remote.apply_patch`

plus jobs (`remote.job_status` / `job_tail` / `job_stop`), artifacts
(`remote.artifact_manifest` / `artifact_pull` / `artifact_push`) and endpoint
facts (`remote.probe`, `remote.context_snapshot`).

Layer B is the shared substrate:

- endpoint identity and explicit resolution (`remote_dev.core.endpoint`)
- resolver plugin interface for consumer-owned selectors (same module)
- SSH transport with ControlMaster reuse, per-endpoint unmultiplexed
  connections, optional ServerAlive keepalive, attached live streams,
  local port forwards, and one-off interactive bootstrap
  (`remote_dev.core.ssh_transport`)
- full-permission default root with optional explicit root/cwd path policy
  (`remote_dev.core.path_policy`, `remote_dev.core.permissions`)
- optional read-ledger concurrency checks (`remote_dev.core.read_ledger`,
  `remote_dev.core.state_store`)
- compact previews plus full refs (`remote_dev.core.preview`, `remote_dev.result`)
- background job registry (`remote_dev.core.job_ops`)
- artifact manifests and pull/push verification (`remote_dev.core.artifact_ops`)
- Claude/Codex hook guards (`remote_dev.hooks`)
- MCP server and resources (`remote_dev.mcp`)

Layer C is whatever the consumer builds on top: workflow skills, session
managers, coordinators. It lives in the consumer's repository and talks to
remote-dev through tool calls, environment variables, and registered resolver
plugins. remote-dev never imports Layer C.

## Dependency direction

The substrate depends on nothing but the Python standard library and an
`ssh` binary. Consumers depend on the substrate. Concretely:

- Endpoint resolution accepts `host`/`port`/`user`/`root`/`cwd`/... and
  `alias`. Every other selector (session ids, machine names, worktree
  bindings) is resolved by a plugin the consumer registers via
  `remote_dev.core.endpoint.register_resolver` or `REMOTE_DEV_RESOLVERS`.
- Consumer facts that used to be constants are environment-configured:
  `REMOTE_DEV_RUNTIME_ENV_FILE` (remote profile script to source),
  `REMOTE_DEV_SSH_MUX_DIR` (shared ControlMaster directory),
  `REMOTE_DEV_STATE_DIR` (where local state lives),
  `REMOTE_DEV_ENDPOINTS_FILE` (alias files).
- Task/coordinator facades are not tools of this server. A consumer that
  wants them runs its own MCP server or wraps this one.

## Implementation phases (historical)

Phase 0 established schemas, result contracts, endpoint identity, path
policy, and hook tests.

Phase 1 implemented `remote.bash`, `remote.read`, and `remote.ls`.

Phase 2 implemented `remote.write`, `remote.edit`, and `remote.multi_edit`
with default write/edit permission, optional read-ledger concurrency checks,
and atomic writes.

Phase 3 implemented `remote.apply_patch` for Codex apply_patch payloads,
including file moves and end-of-file markers, plus unified diffs.

Phase 4 implemented search, monitor/jobs, and artifact manifest/pull/push.

Phase 5 added MCP, client configuration examples, and hook guards.

Phase 6 (this repository) extracted the substrate from its first consumer
and inverted the dependency: explicit endpoints, resolver plugins, no
consumer state, no consumer imports.

## MCP transport

The MCP server supports standard stdio `Content-Length` JSON-RPC framing.
The newline-delimited JSON-RPC mode is retained only as a lightweight local
test fallback.

MCP resources expose endpoint index/context, job registries and bounded
stdout/stderr reads, and local artifact manifests:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/status`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stdout`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stderr`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

## Validation

```bash
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

Remote endpoint behaviour requires a reachable SSH endpoint. Use
`remote-dev validate` with `--host/--port` (or an `--alias`, or
`--selector KEY=VALUE` for a registered resolver) to run the live smoke,
including parallel scratch workers.
