# Handoff: consuming remote-dev from vllm-ascend-workspace

This document records exactly what changed when `.remote-dev/` was extracted
from the `vllm-ascend-workspace` scaffold into this repository, what the
scaffold must change to consume it as an external dependency, and what moved
to the sibling repository `vllm-ascend-workspace/vaws-coordinator`.

Commit references below are commits in this repository. Files removed here are
still retrievable from history, e.g.
`git show <commit>^:core/vaws_ops.py`.

## 1. Extraction

- Source: scaffold `origin/main` at `161fed1b0fe6b48359be3f0cf33bb7d8befae113`.
- Method: `git filter-repo --force --refs main --path .remote-dev/
  --path-rename .remote-dev/:` (no `git subtree` on the extraction host).
- History: 8 commits touched `.remote-dev/` in the scaffold; 8 commits exist
  on `main` here after the rewrite (first commit `73263e5 Add remote-dev
  substrate` through `a6f37bf feat: harden sessions ... (#66)`), then the
  decoupling commits on top. 84 tracked files before and after the rewrite;
  84 distinct paths ever touched in the rewritten history; zero paths outside
  the former `.remote-dev/` tree.
- Files landed at the repository root (`core/`, `mcp/`, `tools/`, ...), not
  under a nested `.remote-dev/`.

## 2. Dependency inversion summary

| Before (inside the scaffold) | After (this repo) |
|---|---|
| `core/endpoint.py` imported `vaws_remote_toolbox.resolve_remote_target` from `<repo>/.agents/lib` | No consumer import anywhere. `resolve_endpoint` handles `host`+`port` and `alias`; everything else goes through `register_resolver` plugins. |
| Selectors `session_id` / `session_file` / `machine` were built into `ENDPOINT_PROPS`, `tools/_cli.py` flags and `mcp/tools.py` checks | Removed. Consumer resolvers declare their own `fields`; CLI passes them with `--selector KEY=VALUE`; schemas stay `additionalProperties: true`. |
| Empty payload auto-bound to the nearest `.vaws-local/current-session.json` | Empty payload is offered to registered resolvers; with none registered it fails with `EndpointError`. |
| `mcp/tools.py` dispatched `vaws.*` to `core/vaws_ops.py::vaws_call` | Removed (see section 4). Unknown tools are rejected before endpoint resolution. |
| `core/shell_ops.py`, `core/job_ops.py` sourced `/etc/profile.d/vaws-ascend-env.sh` | `core/runtime_env.py` sources `Endpoint.runtime_env_file` only when set (payload/alias/resolver/`REMOTE_DEV_RUNTIME_ENV_FILE`). |
| `core/ssh_transport.py` used `~/.ssh/vaws-mux` | `~/.ssh/remote-dev-mux`, overridable with `REMOTE_DEV_SSH_MUX_DIR`. |
| `core/state_store.py` wrote under `.remote-dev/state` | `<checkout>/state`, overridable with `REMOTE_DEV_STATE_DIR`. |
| `endpoints.json` was tracked (empty) | Untracked; `REMOTE_DEV_ENDPOINTS_FILE` adds consumer alias files. |
| `core/managed_jobs.py` (coordinator job supervisor) | Removed (see section 4). |
| `tools/sync_claude_skills.py` (scaffold skill shims) | Removed (see section 3.6). |

The permission default is unchanged: direct endpoints resolve with `root=/`
(full remote-path permission) and cwd `/vllm-workspace` unless the caller or
`REMOTE_DEV_DEFAULT_ROOT` / `REMOTE_DEV_DEFAULT_CWD` says otherwise. The
scaffold's `.mcp.json` and `.cursor/mcp.json` currently set
`REMOTE_DEV_DEFAULT_ROOT=/vllm-workspace`; that keeps working as-is.

## 3. What the scaffold must change

### 3.1 Vendor or install this repository

Pick one and adjust the paths in 3.2-3.5 accordingly:

- **Submodule**: `git submodule add <remote-dev url> .remote-dev` keeps every
  existing `.remote-dev/...` path valid. Remove the old in-tree directory
  first (`git rm -r .remote-dev`).
- **Sibling checkout**: point configuration at an absolute path such as
  `/opt/remote-dev` and export `REMOTE_DEV_ROOT` for scripts that need it.

Keep `.gitmodules` on the community upstream URLs for `vllm/` and
`vllm-ascend/`; adding `remote-dev` as a third submodule does not change
those entries.

### 3.2 Register a resolver plugin (replaces `_endpoint_from_managed`)

Create `.agents/lib/vaws_remote_dev_plugin.py` (name is a suggestion) that
reproduces the removed `core/endpoint.py::_endpoint_from_managed` on the
scaffold side:

```python
"""remote-dev resolver plugin for VAWS managed sessions and machines."""
from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from core.endpoint import DEFAULT_ROOT, Endpoint, EndpointError, register_resolver, resolver_setup
from vaws_remote_toolbox import RemoteToolboxError, resolve_remote_target

REPO_ROOT = LIB.parents[1]
RUNTIME_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"


def resolve_vaws(payload):
    selectors = {k: payload.get(k) for k in ("machine", "session_id", "session_file")}
    try:
        target = resolve_remote_target(repo_root=REPO_ROOT, **selectors)
    except RemoteToolboxError as exc:
        if not any(selectors.values()):
            return None  # no selector and no worktree binding: not ours
        raise EndpointError(f"failed to resolve managed target: {exc}") from exc
    endpoint = target.container_endpoint
    is_session = bool(getattr(target, "session_id", None))
    return Endpoint(
        host=endpoint.host,
        port=int(endpoint.port),
        user=endpoint.user,
        root=str(payload.get("root") or DEFAULT_ROOT),
        cwd=str(payload.get("cwd") or target.runtime_root),
        runtime_env=bool(payload.get("runtime_env", True)),
        runtime_env_file=str(payload.get("runtime_env_file") or RUNTIME_ENV_FILE),
        identity_file=str(payload["identity_file"]) if payload.get("identity_file") else None,
        connect_timeout_ms=int(payload.get("connect_timeout_ms") or 10000),
        kind="managed-session" if is_session else "managed-machine",
        alias=str(payload.get("session_id") or payload.get("machine") or getattr(target, "session_id", None) or target.alias),
        source={"vaws_target": target.to_dict()},
    )


@resolver_setup
def setup():
    register_resolver(resolve_vaws, name="vaws", fields=("machine", "session_id", "session_file"))
```

Notes:

- The auto-bind behaviour ("zero endpoint arguments inside a session
  worktree") is preserved because `resolve_remote_target` with no selector
  already calls `load_session_lookup`, which walks upward for
  `.vaws-local/current-session.json` (bounded at the repo root by
  `vaws_session_id.find_session_binding` / `_binding_walk_stop`). The
  resolver returns `None` when that fails without an explicit selector so
  remote-dev can produce its own "no endpoint target" error.
- The three `FindSessionBindingTests` removed from `tests/test_endpoint.py`
  here test `vaws_session_id.find_session_binding` directly; move them to
  `.agents/tests/` (they never depended on remote-dev code).
- `resolve_remote_target` and `RemoteTarget` are unchanged in
  `.agents/lib/vaws_remote_toolbox.py`.

### 3.3 Update client configuration files

Add the plugin and state wiring to every server entry. With the submodule
layout the `args` path stays `.remote-dev/mcp/server.py`.

`.mcp.json` and `.cursor/mcp.json` (`env` block):

```json
"REMOTE_DEV_DEFAULT_USER": "root",
"REMOTE_DEV_DEFAULT_ROOT": "/vllm-workspace",
"REMOTE_DEV_DEFAULT_CWD": "/vllm-workspace",
"REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh",
"REMOTE_DEV_RESOLVERS": ".agents/lib/vaws_remote_dev_plugin.py:setup",
"REMOTE_DEV_STATE_DIR": ".vaws-local/remote-dev-state",
"REMOTE_DEV_SSH_MUX_DIR": "~/.ssh/vaws-mux"
```

Relative paths in `REMOTE_DEV_RESOLVERS` / `REMOTE_DEV_STATE_DIR` resolve
against the server process cwd, which for `.mcp.json` / `.cursor/mcp.json` is
the project root (the same assumption the existing relative `args` path makes).
`.codex/config.example.toml` and `.grok/config.example.toml` use absolute
placeholder paths; add the same keys under `[mcp_servers.remote_dev.env]` /
`[mcp_servers.remote-dev.env]`. `.agents/scripts/vaws_client_setup.py`
(`entry.update(... args=[str(ROOT / ".remote-dev/mcp/server.py")] ...)` at
lines 55-56 and the Codex TOML body at lines 91-94) must emit the same `env`.

`.claude/settings.example.json`: the hook commands
`python3 .remote-dev/hooks/claude_remote_guard.py` keep working with the
submodule layout. `.codex/config.example.toml` line 27 uses
`$(git rev-parse --show-toplevel)/.remote-dev/hooks/codex_remote_guard.py`,
also unchanged with the submodule layout.

`.gitignore`: `.remote-dev/state/` and `.remote-dev/endpoints.local.json` can
be dropped once `REMOTE_DEV_STATE_DIR` points elsewhere (the submodule has its
own `.gitignore`); add `.vaws-local/remote-dev-state/` if you follow the
suggestion above (`.vaws-local/` is already ignored).

### 3.4 Update Python call sites

| File | Line(s) | Change |
|---|---|---|
| `.agents/coordinator/backend.py` | 12 `sys.path.append(str(ROOT / ".remote-dev"))` | Keep (submodule) or point at the installed checkout. |
| `.agents/coordinator/backend.py` | 14-15 `from core.endpoint import resolve_endpoint`, `from core.shell_ops import remote_bash` | Unchanged API. If `backend.py` passes `session_id`/`machine` payloads to `resolve_endpoint`, import and run `vaws_remote_dev_plugin.setup()` first (or set `REMOTE_DEV_RESOLVERS` in the coordinator process). Endpoints built from inventory rows with explicit `host`/`port` need no plugin. |
| `.agents/coordinator/backend.py` | 26 `(ROOT / ".remote-dev/core/managed_jobs.py").read_text()` | The file no longer exists here. Read it from `vaws-coordinator` (section 4.2). |
| `.agents/coordinator/backend.py` | `RemoteBackend.bash(...)` callers | If they relied on the implicit `/etc/profile.d/vaws-ascend-env.sh` preamble, pass `runtime_env_file` in the endpoint dict or set `REMOTE_DEV_RUNTIME_ENV_FILE`. |
| `.agents/scripts/vaws.py` | 11 `sys.path[:0] = [... str(ROOT / ".remote-dev")]`, 13 `from core.result import make_result`, 14 `from core.vaws_ops import vaws_call`, 72 `vaws_call("vaws." + operation, merged)` | `core.result.make_result` is still available. `core.vaws_ops` is gone; import `vaws_call` from its new home in `vaws-coordinator` (section 4.1). |
| `.agents/lib/vaws_remote_toolbox.py` | 1572-1577, 1721-1726, 1771-1774, 1890 | These build argv for scaffold skill scripts (`parity_sync.py`, `session_*`), not remote-dev CLI wrappers; no change. If anything invokes `.remote-dev/tools/remote_*.py --session-id/--session-file/--machine`, switch to `--selector session_id=...` etc. A repo-wide grep at extraction time found no such call site. |
| `.agents/lib/vaws_remote_toolbox.py` | 522 docstring mentions `.remote-dev/core/` as byte-stable remote surfaces | Wording only. |
| `.agents/skills/session-management/tests/test_agent_sessions.py` | 165, 178-202 (`mcp__remote_dev__vaws_session`, `remote-dev__vaws_session`) | These test the scaffold hook `.agents/hooks/vaws_session.py` rewriting tool names. They stay valid only if the `vaws_*` tools are served again under the `remote-dev` MCP server name by `vaws-coordinator`; otherwise update the expected names to the coordinator's server id. |
| `.agents/hooks/vaws_session.py` | 79, 98 | Same as above: the hook injects `context_file` for `vaws_session/vaws_run/vaws_execution/vaws_finish`. Point it at whichever MCP server now exposes them. |

### 3.5 Update CI and docs

- `.github/workflows/remote-dev.yml`: drop the `.remote-dev/**` paths and the
  `python -B -m unittest discover -s .remote-dev/tests` step; this repo's
  `.github/workflows/ci.yml` runs the same suite. Keep a scaffold job that
  runs `python3 -m unittest discover -s .agents/tests` plus the moved
  `FindSessionBindingTests`, and a smoke that imports
  `vaws_remote_dev_plugin` and resolves a fake session binding through
  `core.endpoint.resolve_endpoint`.
- `AGENTS.md` line 36 ("Managed VAWS `session_id`, `session_file`, and
  `machine` remain available as compatibility modes") and `README.md`: state
  that those selectors are provided by the scaffold's resolver plugin, and
  that `--session-id/--session-file/--machine` on remote-dev CLI wrappers
  became `--selector KEY=VALUE`.
- `.remote-dev/README.md`'s former paragraph about the `vaws_*` task facade
  moves to the coordinator's README.

### 3.6 Re-home `sync_claude_skills.py`

`tools/sync_claude_skills.py` (removed in commit `900ad15`) generated
`.claude/skills/<name>/SKILL.md` shims from `.agents/skills/<name>/SKILL.md`.
Move it to `.agents/scripts/sync_claude_skills.py`, change
`ROOT = Path(__file__).resolve().parents[2]` (it assumed
`.remote-dev/tools/`), and update:

- `.remote-dev/DESIGN.md` / `README.md` / `VALIDATION.md` references to
  `python3 .remote-dev/tools/sync_claude_skills.py --check`;
- the two tests removed from `tests/test_cli_help.py`
  (`test_claude_skill_shim_check_passes`,
  `test_claude_skill_shim_check_reports_unexpected_files`) and
  `test_claude_skills_are_lightweight_shims` -> `.agents/tests/`;
- the generated shim text, which says "Use `.remote-dev` companion tools for
  ordinary remote endpoint read/edit/bash/search/patch work" (still true with
  the submodule layout).

## 4. What moves to `vllm-ascend-workspace/vaws-coordinator`

### 4.1 VAWS task facade (removed in commit `f30b992`)

Retrieve with `git show f30b992^:core/vaws_ops.py` and
`git show f30b992^:mcp/schemas.py`, `git show f30b992^:mcp/tools.py`.

- **Module** `core/vaws_ops.py`: `ROOT = Path(__file__).resolve().parents[2]`
  (the scaffold root when the file lived at `.remote-dev/core/`), and
  `vaws_call(name, args) -> {"text": str, "result": dict}`. Behaviour:
  lazily inserts `ROOT/.agents/lib` into `sys.path`, imports
  `vaws_task_client.TaskClient`, constructs
  `TaskClient(args.get("context_file", ""))`, sets
  `target = {"kind": "vaws-task", "session_id": client.context["session"]["id"]}`,
  then dispatches:
  - `vaws.session`: optional `client.sources(args["sources"])`, then
    `client.status()`; status is `value["session"]["state"]`.
  - `vaws.run`: `client.run(**{k: args[k] for k in ("request_id", "command",
    "profile_key", "runtime_id", "devices", "npu_count", "env",
    "timeout_seconds") if k in args})`; status `value["state"]`.
  - `vaws.execution`: `client.observe(args["execution_id"],
    args.get("action", "status"), args.get("force", False))`.
  - `vaws.finish`: `client.finish(args.get("force", False))`.
  - Outcome mapping: `blocked` for status in `{"uncertain",
    "waiting_for_runtime"}`, `failed` for `"failed"`, `timeout` for
    `"timeout"`, else `success`; **except** `vaws.finish` with a non-`finished`
    status (e.g. `finishing`) is downgraded to `blocked`.
  - Any exception -> `make_result(outcome="blocked", status="unavailable",
    summary=str(exc), warnings=["Local file and shell tools remain available.
    No remote success is implied."])`.
  - Results use `core.result.make_result` with `tool=name`, `summary="VAWS "
    + status.replace("_", " ")`, `extra={"data": value}`.
- **Schemas** (`mcp/schemas.py`): `task_schema(properties, required=())`
  producing `{"type": "object", "properties": {"context_file": {"type":
  "string", "description": "Local task context supplied by the native session
  hook; never guess from cwd or newest history."}, **properties},
  "required": [...], "additionalProperties": False}` and the entries:
  - `vaws.session`: `sources: object<string>`.
  - `vaws.run`: `request_id: string`, `command: string`, `profile_key:
    string`, `runtime_id: string`, `devices: integer[]`, `npu_count: integer
    (default 1)`, `env: object<string>`, `timeout_seconds: integer (default
    1800)`; required `request_id`, `command`.
  - `vaws.execution`: `execution_id: string`, `action: enum[status, tail,
    stop]`, `force: boolean`; required `execution_id`.
  - `vaws.finish`: `force: boolean`.
  - Wire names via `ALIASES`: `vaws_session`, `vaws_run`, `vaws_execution`,
    `vaws_finish`.
- **Descriptions** (`mcp/tools.py::list_tools`):
  - `vaws.session`: "Inspect this native session's VAWS task and bind actual
    business worktrees. Local only: no machine or coordinator is required.
    Use the context_file supplied by the session hook."
  - `vaws.run`: "Run the task's current source snapshot on a compatible
    prepared runtime. Automatically sync, acquire devices, launch, renew,
    observe and release; never install packages or create containers. Keep
    request_id unchanged on retry."
  - `vaws.execution`: "Observe, tail or stop one execution belonging to this
    VAWS task. Other tasks cannot be selected accidentally. Stop confirms
    process and NPU release."
  - `vaws.finish`: "Finish this VAWS task by stopping only its owned
    executions and releasing resources; preserve worktrees and evidence."
- **Dispatch** (`mcp/tools.py::call_tool`): `if name.startswith("vaws."):
  return vaws_call(name, args)` before any endpoint resolution.
- **Tests**: `tests/test_vaws_ops.py` (2 tests, patch
  `vaws_task_client.TaskClient` with a fake whose `finish()` returns
  `{"state": ..., "executions": [], "worktrees_preserved": True}`):
  `test_finish_non_terminal_state_is_blocked_not_success`,
  `test_finish_terminal_state_stays_success`. From
  `tests/test_cli_help.py`:
  `test_task_facade_uses_one_cli_without_endpoint_or_network_requirements`
  (`--help` matrix for `.agents/scripts/vaws.py` and its `attach/session/
  run/execution/finish` subcommands),
  `test_json_arguments_are_not_overridden_by_argparse_defaults`,
  `test_vaws_cli_bad_json_returns_result_contract_without_traceback`
  (expects `tool=vaws.session`, `status=invalid_json`,
  `outcome=needs_input`),
  `test_vaws_cli_attach_error_returns_result_contract_without_traceback`
  (expects `tool=vaws.attach`, `status=attach_failed`, `outcome=failed`),
  and `test_vaws_ops_import_defers_agents_lib_dependency` (importing the
  module must not touch `.agents` in `sys.path`).
- **Consumers to rewire**: `.agents/scripts/vaws.py` (imports `vaws_call`),
  `.agents/hooks/vaws_session.py` (injects `context_file` for the four tool
  names), `.agents/coordinator/README.md` lines 125-160 (documents the four
  tools "on the existing remote-dev stdio server"), the session-management
  hook tests listed in 3.4.

Options for serving them: run a second stdio MCP server from
`vaws-coordinator` that imports `core.result.make_result` from remote-dev, or
wrap `mcp.server` with a dispatcher that handles `vaws.*` first and delegates
`remote.*` to `mcp.tools.call_tool`. remote-dev will not grow a plugin hook
for foreign tools; the tool surface here is remote development only.

### 4.2 Managed job supervisor (removed in commit `900ad15`)

Retrieve with `git show 900ad15^:core/managed_jobs.py` and
`git show 900ad15^:tests/test_managed_jobs.py`.

- **Module** `core/managed_jobs.py` ("Linux remote job receipt protocol used
  by the VAWS execution supervisor"). Self-contained; stdlib only; shipped to
  the remote host as source text. Public surface:
  - `control_job(request, source)` with `request = {"root", "job_id",
    "action", ...}`; `job_id` must match `vaws-[a-f0-9]{64}`; job dir is
    `<root>/.vaws-runtime/remote-dev/jobs/<job_id>` (must stay under
    `root`); actions `prepare` (`spec = {"cwd", "command", "env",
    "timeout_seconds" in 1..86400}`; env keys must match
    `[A-Za-z_][A-Za-z0-9_]*` and not start with `VAWS_REMOTE_JOB_`; idempotent
    by spec digest in `intent.json`; spawns `python3 runner.py --worker <dir>`
    with `VAWS_REMOTE_JOB_TOKEN=<marker>` in a new session and waits for
    `supervisor-ready.json`), `go` (`authorization` must match any existing
    `go.json`; requires state `prepared`; writes `go.json` with
    `opened_at`/`valid_until = +30s`), `stop` (`force` optional; signals only
    observed marker-owned PIDs, never the subreaper), `tail` (`lines` 1..200,
    last 32000 bytes of `stdout.log`/`stderr.log`), `status`.
  - `job_status(directory)` returns `{"state", "quiet", "receipt",
    "processes", "unknown", "result", "remote_dir", "gate_open"}`; states
    `prepared`, `running`, `succeeded`, `failed`, `timeout`, `cancelled`,
    `uncertain`, `lost_outcome`, `absent`; `receipt.process_guard =
    {"marker", "boot_id", "retain_until_release"}` is consumed by
    `.agents/skills/session-management/scripts/npu_coordination.py` /
    `vaws_npu_coordination.process_guard_busy`.
  - `worker(directory)`: `prctl(PR_SET_CHILD_SUBREAPER)`, waits up to 120 s
    for `go.json`, runs `bash -c spec["command"]` with `spec["env"]`,
    enforces `timeout_seconds`, drains adopted orphans, publishes
    `result.json = {"state", "exit_code", "descendants_drained",
    "finished_at"}`.
  - Helpers `atomic_json`, `read_json`, `boot_id`, `process_identity`,
    `owned_processes`, `signal_processes`.
  - `__main__`: `--worker <dir>` runs the worker; otherwise expects the
    wrapper to have injected `WORKER_SOURCE` and prints
    `json.dumps(control_job(json.loads(argv[1]), WORKER_SOURCE))`, exiting
    with "WORKER_SOURCE is undefined: ..." when it was not injected.
- **Caller**: `.agents/coordinator/backend.py::RemoteBackend.job` reads the
  file, wraps it as
  `python3 - <json request> <<'VAWS_MANAGED_JOB'\nWORKER_SOURCE = <repr(source)>\nexec(compile(WORKER_SOURCE, '<vaws-managed-job>', 'exec'))\nVAWS_MANAGED_JOB`
  and runs it through `core.shell_ops.remote_bash`. Only the read path
  changes; the remote-dev transport it rides on is unchanged.
- **Tests**: `tests/test_managed_jobs.py` - `ManagedJobTests` (7 tests,
  `@unittest.skipUnless(sys.platform == "linux")`, import
  `vaws_npu_coordination.process_guard_busy` from `.agents/lib`):
  `test_waiting_gate_is_idempotent_and_command_does_not_run_early`,
  `test_stop_clean_environment_daemon_keeps_the_other_family_alive`,
  `test_stop_before_go_never_executes_and_unknown_receipt_is_not_free`,
  `test_timeout_is_not_reported_as_a_success_or_manual_cancel`,
  `test_background_descendant_cannot_outlive_the_bounded_execution_unobserved`,
  `test_lost_supervisor_cannot_report_quiet_or_release_its_retained_guard`,
  `test_legacy_receipt_stop_without_result_reports_cancelled`; and
  `ManagedJobEntrypointTests.test_main_without_wrapper_injected_worker_source_fails_with_clear_message`
  (runs on any platform). They ran as skipped on macOS and as real process
  checks in Linux CI.

## 5. Tests changed in this repository

Removed (moved with their subject):

- `tests/test_vaws_ops.py` (2) -> vaws-coordinator.
- `tests/test_managed_jobs.py` (8) -> vaws-coordinator.
- `tests/test_endpoint.py::FindSessionBindingTests` (3) -> scaffold
  `.agents/tests` (they test `vaws_session_id`).
- `tests/test_cli_help.py`:
  `test_task_facade_uses_one_cli_without_endpoint_or_network_requirements`,
  `test_json_arguments_are_not_overridden_by_argparse_defaults`,
  `test_vaws_cli_bad_json_returns_result_contract_without_traceback`,
  `test_vaws_cli_attach_error_returns_result_contract_without_traceback`
  (4) -> vaws-coordinator (they exercise `.agents/scripts/vaws.py`);
  `test_claude_skill_shim_check_passes`,
  `test_claude_skill_shim_check_reports_unexpected_files`,
  `test_claude_skills_are_lightweight_shims` (3) -> scaffold with
  `sync_claude_skills.py`.

Rewritten (behaviour still exists here, target changed):

- `tests/test_cli_help.py::test_vaws_ops_import_defers_agents_lib_dependency`
  -> `test_core_imports_do_not_reach_outside_the_checkout` (imports
  `mcp.tools`, `mcp.server`, `core.endpoint` in a fresh interpreter and
  asserts `sys.path` gained nothing outside the checkout).
- `tests/test_mcp_schema.py::test_cursor_entry_uses_the_shared_server_and_environment`
  -> `test_example_mcp_entry_points_at_the_server_and_documents_consumer_wiring`
  (checks `examples/mcp.json`).
- `tests/test_mcp_schema.py::test_missing_endpoint_is_rejected_before_tool_execution`
  now clears the resolver registry instead of patching
  `vaws_remote_toolbox.resolve_remote_target`.
- `tests/test_mcp_schema.py::test_normal_tools_describe_endpoint_selector_requirement`
  drops the `vaws.*` exemption.
- `tests/test_hook_guard.py::test_claude_settings_hooks_mcp_remote_tools`
  -> `test_claude_settings_example_hooks_mcp_remote_tools` (reads
  `examples/claude-settings.example.json`).
- `tests/test_cli_help.py::test_cli_wrappers_have_help` no longer filters
  `TOOL_SCHEMAS` by the `remote.` prefix (there is nothing else).

Added:

- `tests/test_endpoint.py`: `AliasFileTests` (3), `ResolverPluginTests` (9),
  `EnvResolverLoadingTests` (4), plus port/`runtime_env_file` checks (2).
- `tests/test_cli_help.py`: `test_cli_endpoint_flags_are_explicit_only`,
  `test_cli_selector_without_resolver_is_endpoint_required`,
  `test_cli_bad_selector_item_is_invalid_input`.
- `tests/test_mcp_schema.py`: `test_only_remote_tools_are_advertised`,
  `test_endpoint_props_carry_no_consumer_selectors`,
  `test_consumer_selector_reaches_registered_resolver_through_call_tool`,
  `test_job_tools_only_resolve_when_a_selector_is_present`,
  `test_unknown_tool_is_rejected_before_endpoint_resolution`.
- `tests/test_remote_bash.py`: `test_runtime_env_preamble_is_explicit_per_endpoint`,
  `test_background_job_records_runtime_env_file_and_restores_it`.
- `tests/test_ssh_transport.py`: `test_mux_dir_defaults_under_home_and_honours_env_override`.
- `tests/test_read_ledger.py`: `StateRootTests` (1).

Suite size: 118 tests (7 skipped on macOS) inside the scaffold before
extraction; 128 tests, 0 skipped, on the standalone checkout afterwards.

## 6. Sensitive data audit of the extracted history

The rewritten history (8 pre-extraction commits) contains no credentials,
tokens or container names, but it does contain endpoint-like data in
documentation from the 2026-05-25 validation runs and one commit message:

- Two public-range host IP addresses used as validation targets in earlier
  revisions of `VALIDATION.md` and in the `README.md` usage example; the
  current tree replaces them with placeholders.
- One RFC 1918 host IP in the body of the `feat: harden sessions ... (#66)`
  commit message.
- Absolute user paths (`/Users/<user>/code/vaws-worktrees/...`,
  `/Users/<user>/Downloads/...`) in earlier revisions of `VALIDATION.md` and
  `DESIGN.md`; the current tree has none.
- Author e-mail addresses in commit metadata (normal for any Git history).

Because of this the repository was created **private**. To publish it, rewrite
the history once more with `git filter-repo --replace-text <rules>` (rules
mapping the three IPs and the `/Users/<user>` prefix to placeholders and
`--replace-message` for the commit body), re-verify with `git log -p | grep`,
and only then flip visibility. Do that before anyone clones it, since it
changes every commit id.

## 7. Remaining scaffold flavour (not coupling)

These defaults and probe fields come from the first consumer. They do not
reference scaffold files or state, are all overridable, and were kept so the
scaffold keeps identical behaviour without configuration:

- `REMOTE_DEV_DEFAULT_CWD` default `/vllm-workspace`.
- `core/context_snapshot.py::REMOTE_PROBE_PY` reports `vllm_head` /
  `vllm_ascend_head` when `<root>/vllm` and `<root>/vllm-ascend` exist, probes
  the `torch`, `torch_npu`, `vllm`, `vllm_ascend` modules, and lists
  `npu_state: volatile` in the snapshot's volatility map. Harmless on hosts
  without them (reported as unavailable); a follow-up could make the module
  and directory lists configurable.
- `CLIENT_COMPATIBILITY.md` and `VALIDATION.md` keep the 2026-08-27 and
  2026-05-25 evidence gathered inside the scaffold, clearly labelled as such.
