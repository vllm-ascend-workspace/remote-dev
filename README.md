# remote-dev

A remote development substrate for coding agents. It makes a remote Linux
host reachable over SSH feel like the local working tree: every native editor
tool has a remote twin with the same semantics plus endpoint fields.

Install the `vaws-remote-dev` package. The import package is `remote_dev`;
the console entry is `remote-dev`.

| Local tool  | Remote tool          | CLI                                      |
|-------------|----------------------|------------------------------------------|
| Read        | `remote.read`        | `remote-dev read`                        |
| Write       | `remote.write`       | `remote-dev write`                       |
| Edit        | `remote.edit`        | `remote-dev edit`                        |
| MultiEdit   | `remote.multi_edit`  | `remote-dev multi-edit`                  |
| Bash        | `remote.bash`        | `remote-dev bash`                        |
| Glob        | `remote.glob`        | `remote-dev glob`                        |
| Grep        | `remote.grep`        | `remote-dev grep`                        |
| LS          | `remote.ls`          | `remote-dev ls`                          |
| Monitor     | `remote.monitor`     | `remote-dev monitor`                     |
| apply_patch | `remote.apply_patch` | `remote-dev apply-patch`                 |

Plus background jobs (`remote.job_status` / `job_tail` / `job_stop`),
artifacts (`remote.artifact_manifest` / `artifact_pull` / `artifact_push`),
and endpoint facts (`remote.probe`, `remote.context_snapshot`). Eighteen
tools in total, served by one stdio MCP server (`remote-dev server`) and
mirrored one-to-one by CLI subcommands. `python -m remote_dev` is equivalent
to `remote-dev`.

Runtime requirements: Python 3.9+ and an `ssh` client. No third-party
packages. Nothing here needs GPU/NPU hardware; the remote host only needs
`bash`, `python3`, and (for `remote.apply_patch` unified diffs) `git`.

## Install and start the MCP server

From a git ref (no local checkout required):

```bash
uvx --from git+https://github.com/vllm-ascend-workspace/remote-dev@main remote-dev server
```

From a clone:

```bash
uv pip install -e .
remote-dev server
```

`.mcp.json` (Claude Code / Kimi / Cursor):

```json
{
  "mcpServers": {
    "remote-dev": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vllm-ascend-workspace/remote-dev@main",
        "remote-dev",
        "server"
      ],
      "env": {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_DEFAULT_ROOT": "/",
        "REMOTE_DEV_DEFAULT_CWD": "/vllm-workspace",
        "REMOTE_DEV_RESOLVERS": "/absolute/path/to/consumer/remote_dev_plugin.py:setup"
      }
    }
  }
}
```

If the package is already installed in the client environment, `command` can
be `remote-dev` with `args: ["server"]`. More client examples live in
`examples/`. See [CLIENT_COMPATIBILITY.md](CLIENT_COMPATIBILITY.md) for
per-client notes.

## The endpoint-explicit contract

remote-dev resolves endpoints from explicit fields and nothing else:

| Field                | Default              | Meaning                                              |
|----------------------|----------------------|------------------------------------------------------|
| `host`               | required             | SSH host                                             |
| `port`               | required             | SSH port                                             |
| `user`               | `root`               | SSH user (`REMOTE_DEV_DEFAULT_USER`)                 |
| `root`               | `/`                  | Path-policy root (`REMOTE_DEV_DEFAULT_ROOT`)         |
| `cwd`                | `/vllm-workspace`    | Default working dir (`REMOTE_DEV_DEFAULT_CWD`)       |
| `runtime_env`        | `true`               | Source `runtime_env_file` before commands            |
| `runtime_env_file`   | unset                | Remote profile script (`REMOTE_DEV_RUNTIME_ENV_FILE`)|
| `identity_file`      | unset                | SSH private key                                      |
| `connect_timeout_ms` | `10000`              | SSH connect timeout                                  |
| `alias`              | unset                | Name from the endpoint alias files                   |

Resolution order in `remote_dev.core.endpoint.resolve_endpoint`:

1. `host` + `port` given: use them directly.
2. `alias` given: look it up in the alias files (`REMOTE_DEV_ENDPOINTS_FILE`,
   then `endpoints.json` and `endpoints.local.json` in the process cwd; both
   are git-ignored) and let explicit caller fields override the alias entry.
3. Otherwise ask each registered resolver plugin, in registration order.
4. Nothing claimed the payload: fail with `EndpointError` listing the known
   selector fields and registered resolvers. remote-dev never guesses a
   target from the working directory or from files it does not own.

`remote.job_status`, `remote.job_tail` and `remote.job_stop` can rebuild
their endpoint from the local job record and therefore resolve only when the
caller supplies a selector.

### Permission model

Direct endpoints default to **full remote-path permission** (`root=/`) with
`/vllm-workspace` as the default cwd. Path containment, symlink checks and
cwd validation are still enforced, but against `/`. Pass a narrower `root`
(and usually the same `cwd`) when a task requires path isolation, e.g.
`--root /srv/app --cwd /srv/app`. This is a deliberate, documented default:
the tools exist to replace ad-hoc `ssh` invocations that had no containment
at all, and a consumer that wants a tighter default sets
`REMOTE_DEV_DEFAULT_ROOT`. Hook guards (`remote_dev.hooks`) default to *allow*
and only observe; they are the place to add policy if you need it.

Read ledgers are optional optimistic-concurrency checks scoped by
`client_context_id`, then `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`,
`CODEX_RUN_ID`, `REMOTE_DEV_SESSION_ID`. The MCP server sets a process-local
`REMOTE_DEV_SESSION_ID` at startup. Each nonempty effective context id is
stored under one filesystem-safe directory `id-<sha256 of the raw id>`. The
no-context fallback remains `default` and does not collide with a caller who
explicitly supplies that word. A ledger is never required for an edit or
write of a file this context has not previously read. After a successful
read, later writes in that context are checked against the recorded SHA.
Ledgers written by earlier encodings are kept in place. A write or edit that
finds only those older files — including a v1 record occupying the current
`id-<sha256>` or `default` path — returns structured `read_required` until
the same context reads the file again. Path or `ledger_scope` matching the
current computed string is not enough: only a record written under the
current encoding (schema `remote-dev.read_ledger.v2`) authorizes a later
write. A legacy shared SHA is not authorization for a distinct context.

## Resolver plugin interface

Anything that is not `host`/`port`/`alias` - session registries, machine
inventories, worktree bindings, coordinator leases - is consumer knowledge.
The consumer registers a resolver with remote-dev; remote-dev never imports
the consumer.

```python
# consumer/remote_dev_plugin.py
from remote_dev.core.endpoint import EndpointError, register_resolver, resolver_setup

def by_session(payload):
    session_id = payload.get("session_id")
    if not session_id:
        return None                      # not ours: next resolver, please
    record = my_registry.load(session_id)  # consumer-owned lookup
    if record is None:
        raise EndpointError(f"unknown session {session_id!r}")
    return {
        "host": record.host, "port": record.ssh_port,
        "cwd": record.runtime_root,
        "runtime_env_file": "/etc/profile.d/toolchain.sh",
        "kind": "managed-session",
        "source": {"session_id": session_id},
    }

@resolver_setup
def setup():
    register_resolver(by_session, name="sessions", fields=("session_id",))
```

Contract:

- `resolve(payload) -> dict | Endpoint | None`. Return `None` to decline.
  A `dict` needs `host` and `port`; remote-dev builds the `Endpoint`, lets
  explicit caller fields (`user`, `root`, `cwd`, `runtime_env`,
  `runtime_env_file`, `identity_file`, `connect_timeout_ms`) override the
  resolver's values, sets `kind` to `resolver:<name>` unless provided, and
  records `source.resolver`.
- `fields` declares the payload keys the resolver claims. They are added to
  `selector_fields()` so `has_selector()` and the job tools treat them as
  "an endpoint was requested". Tool schemas keep
  `additionalProperties: true`, so consumer keys pass through MCP clients
  untouched.
- Resolvers are consulted even for an empty payload. A consumer that wants
  "zero-argument inside my worktree" behaviour implements it in its resolver
  (return `None` when there is no binding).
- Raise `EndpointError` when you claim a payload but cannot resolve it.
  Other exceptions are wrapped into `EndpointError` with the resolver name.

Loading into a process you do not control (the MCP server is spawned by the
client, CLI wrappers by a shell):

```
REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
REMOTE_DEV_RESOLVERS="consumer.remote_dev_plugin:setup,other.pkg:setup"
```

Entries are `module:callable` or `/path/file.py:callable`. A callable marked
with `@resolver_setup` is invoked once and registers what it likes; any other
callable is registered directly under its spec string. A broken entry fails
every resolution with the same message instead of degrading to "no
resolvers". `examples/resolver_plugin.py` is a complete runnable example.

Programmatic embedding works too: `from remote_dev.core.endpoint import
register_resolver` before invoking `remote_dev.mcp.tools.call_tool`.

The public result envelope is `remote_dev.result` (`schema_version`:
`remote-dev.result.v1`). Schema JSON ships as package data.

Selector keys on the CLI travel through `--selector KEY=VALUE`:

```bash
remote-dev bash --selector session_id=abc --command 'nproc'
```

## What this repository does not hold

- No consumer state. `state/` (job records, read ledgers, logs, artifact
  manifests) is git-ignored and relocatable with `REMOTE_DEV_STATE_DIR`.
  The default is `<cwd>/state`.
- No endpoint data. `endpoints.json` / `endpoints.local.json` are
  git-ignored; ship aliases from your own tree via
  `REMOTE_DEV_ENDPOINTS_FILE`. `examples/endpoints.example.json` shows the
  shape.
- No knowledge of any particular consumer: no session registries, machine
  inventories, coordinators, task facades, or skill catalogues. Those live in
  the consumer and reach remote-dev only through resolvers and environment
  variables listed below.

## Configuration reference

| Variable                        | Purpose                                                   |
|---------------------------------|-----------------------------------------------------------|
| `REMOTE_DEV_DEFAULT_USER`       | Default `user` (`root`)                                   |
| `REMOTE_DEV_DEFAULT_ROOT`       | Default `root` (`/`)                                      |
| `REMOTE_DEV_DEFAULT_CWD`        | Default `cwd` (`/vllm-workspace`)                         |
| `REMOTE_DEV_RUNTIME_ENV_FILE`   | Default `runtime_env_file` (unset = no preamble)          |
| `REMOTE_DEV_RESOLVERS`          | Comma-separated resolver plugin specs                     |
| `REMOTE_DEV_ENDPOINTS_FILE`     | Alias file(s), `os.pathsep` separated, read first         |
| `REMOTE_DEV_STATE_DIR`          | Local state directory (default `<cwd>/state`)             |
| `REMOTE_DEV_SSH_MUX_DIR`        | OpenSSH ControlMaster dir (default `~/.ssh/remote-dev-mux`)|
| `REMOTE_DEV_SSH_MUX`            | Process SSH multiplexing: unset or `1` uses the shared ControlMaster; `0` forces independent connections; other values error |
| `REMOTE_DEV_SESSION_ID`         | Read-ledger scope when no client id is given              |

`REMOTE_DEV_SSH_MUX` is process-scoped and is read without changing global SSH
configuration or the shared ControlMaster socket. Leave it unset or set it to
`1` to keep today's shared-mux path, including the per-identity `ControlPath`
suffix. Set it to exact `0` in a CLI process that must not join the shared
master (`ControlMaster=no`, `ControlPath=none`, `ControlPersist=no` on every
SSH invocation from that process). Accepted values are unset, `1`, and `0`;
any other value is a configuration error. Ordinary serving and parity calls
keep the default shared mux; a caller that needs independent connections must
set `0` in that process.

## MCP server and clients

`remote-dev server` speaks JSON-RPC over stdio with `Content-Length` framing
(newline-delimited JSON is accepted as a test fallback). Discovery advertises
portable underscore names (`remote_read`, ...); dotted canonical names remain
accepted on `tools/call`. Resources:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/{status,stdout,stderr}`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

Every tool returns `{"text": ..., "result": ...}` where `result` follows
`remote_dev.result` / the packaged `result.schema.json` (`remote-dev.result.v1`):
`outcome` in `success | needs_input | blocked | failed | timeout | cancelled`,
a compact `preview`, and `refs` to full logs on disk. Model-visible text is
capped; full output is reachable through refs and MCP resources.

## Validation

```bash
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

Live checks need a reachable SSH host:

```bash
remote-dev validate --host <host> --port <port> --root /srv/app --cwd /srv/app
remote-dev validate --selector session_id=<id> --skip-local
```

The validator compile-checks the installed package, reports MCP/CLI burden
metrics, and (with an endpoint) exercises read/edit/write/bash/search, patches,
artifacts, background jobs, MCP resources and parallel scratch workers, then
cleans up after itself.

## Layout

```
remote_dev/  installable package (core, mcp, hooks, tools, schemas)
tests/       unittest suite collected by pytest (mocked transports, no SSH)
examples/    client configs, alias file shape, resolver plugin
docs/        design notes and historical validation evidence
```

See [DESIGN.md](DESIGN.md) for the architecture and
[VALIDATION.md](VALIDATION.md) for the evidence record.

## License

MIT. See [LICENSE](LICENSE).
