# Remote-Dev Scaffold Validation

Last updated: 2026-05-25.

## Current Evidence

- Local contract gates pass:
  - `python3 -m compileall -q .remote-dev .agents`
  - `python3 -m unittest discover -s .remote-dev/tests` -> 53 tests
  - `python3 -m unittest discover -s .agents/tests` -> 15 tests
  - `python3 .remote-dev/tools/sync_claude_skills.py --check`
  - `git diff --check -- .remote-dev .agents AGENTS.md CLAUDE.md .mcp.json .codex .claude .gitignore`
- `validate_remote_dev_scaffold.py --local-only` passes and reports:
  - 18 MCP tools
  - 18 CLI fallbacks
  - no endpoint selector fields required by tool schemas
  - max tool-specific required fields: 3
- Direct live endpoint validation passed on `192.0.2.11:46000` with 3 parallel
  scratch workers. Covered probe, context snapshot, bash success/failure/timeout,
  cwd guards, read/edit/write, ls, glob, grep, apply_patch, artifact
  manifest/pull/push, background jobs, MCP job stdout resource, MCP artifact
  manifest resource, and cleanup.
- Two managed VAWS sessions were created concurrently on `192.0.2.11`:
  - `remote-dev-val-a-20260525t150321z`: worktree
    `/Users/dev/code/vaws-worktrees/vllm-ascend-workspace/remote-dev-val-a-20260525t150321z`,
    container `vaws-maoxx241-remote-dev-val-a-20260525t150321z`, SSH port
    `46002`.
  - `remote-dev-val-b-20260525t150321z`: worktree
    `/Users/dev/code/vaws-worktrees/vllm-ascend-workspace/remote-dev-val-b-20260525t150321z`,
    container `vaws-maoxx241-remote-dev-val-b-20260525t150321z`, SSH port
    `46001`.
- Both sessions passed `validate_remote_dev_scaffold.py --session-id ...` with
  2 parallel scratch workers each.
- Repo-root `.vaws-local/current-session.json` hash stayed unchanged:
  `2d6fdc38c2fae31b165177210ccbfb974863777d7b7d6273edbdcb18b9146525`.
- Both scratch sessions were removed with container, worktree, and lease cleanup;
  validation session records now show `status=removed`, and lease maps are empty.
- Two NPU-leased managed sessions were created concurrently on `192.0.2.12`
  for heavy parallel validation:
  - `remote-dev-heavy-a-20260525t151831z`: container SSH port `46004`,
    leased NPU `0`.
  - `remote-dev-heavy-b-20260525t151831z`: container SSH port `46003`,
    leased NPU `1`.
- The two heavy sessions passed `remote-code-parity` in `source-only` and
  `materialize` modes from distinct local worktrees. Both runs used isolated
  workspace ids, cache lock paths, and manifest paths. Distinct worktree
  markers were materialized and verified remotely:
  - A: `remote_dev_parity_marker.txt` contained
    `session=remote-dev-heavy-a-20260525t151831z`, root commit `63cc52f`.
  - B: `remote_dev_parity_marker.txt` contained
    `session=remote-dev-heavy-b-20260525t151831z`, root commit `9185598`.
- Parallel service lifecycle passed with `/home/weights/Qwen3-0.6B`:
  - A: ready on device `0`, port `30001`, pid `1907`; stopping A left B ready.
  - B: ready on device `1`, port `30000`, pid `1909`; after A stopped, B still
    reported `alive=true`, `health=true`, and `models_ok=true`.
- Parallel benchmark passed in both sessions with a tiny random workload
  (`num_prompts=2`, `max_concurrency=1`, `input_len=8`, `output_len=8`):
  - A result:
    `.vaws-local/sessions/remote-dev-heavy-a-20260525t151831z/benchmark/runs/2026-05-25T15-41-06Z_remote-dev-heavy-a-20260525t151831z_5767_0061f767.json`,
    status `ok`, output throughput `2.1291903226289413`.
  - B result:
    `.vaws-local/sessions/remote-dev-heavy-b-20260525t151831z/benchmark/runs/2026-05-25T15-41-23Z_remote-dev-heavy-b-20260525t151831z_5768_eb1b7315.json`,
    status `ok`, output throughput `2.2523570734077794`.
  - After benchmark cleanup, both sessions reported `service_alive.ok=false`
    and `live_leases.service_ports=[]`.
- Parallel profiling collection passed in both sessions with the same tag
  `remote-dev-same-tag`, proving run directories do not collide:
  - A manifest:
    `.vaws-local/ascend-profiling-collection/runs/20260525_104231_remote-dev-same-tag_remote-dev-heavy-a-20260525t151831z_9949_90e7b6f3/manifest.json`.
  - B manifest:
    `.vaws-local/ascend-profiling-collection/runs/20260525_104231_remote-dev-same-tag_remote-dev-heavy-b-20260525t151831z_9950_a6f4b142/manifest.json`.
  - Both manifests ended with `status=ok`, `workload_status.status=ok`,
    `rank_count=1`, `analysis_status=ok`, and verified
    `kernel_details.csv` plus `trace_view.json`.
- Final host probe on `192.0.2.12` after benchmark and profiling cleanup
  showed all 8 NPU devices free and no busy HBM entries.
- The two heavy sessions were removed with container, worktree, and lease
  cleanup. Post-cleanup status showed `status=removed`, both worktree paths
  absent, and central leases for `192.0.2.12` empty.

## Fixes Made During Validation

- CLI fallback errors now return a JSON `remote-dev.result.v1` result instead of
  leaking tracebacks.
- `remote.apply_patch` schema now requires either `patch` or `command`.
- Artifact pull blocks unsafe manifest relpaths before writing local files.
- Hook wrappers are covered by subprocess tests for Claude exit `2` and Codex
  deny JSON shape.
- Unified `remote.apply_patch` records before sha and real diffstat.
- Remote toolbox explicit `--job-id` duplicates are blocked before remote process
  launch; non-ok `remote_job_start.py` statuses now exit nonzero.
- Added `validate_remote_dev_scaffold.py` as a repeatable JSON-reporting local
  and live validation entry point.
- Memory profiling and profiling collection run directories now include safe
  tags, target/session identity, pid, and a uuid suffix instead of only
  second-level timestamp plus tag.
- Benchmark results are now persisted under session-local
  `.vaws-local/sessions/<session-id>/benchmark/runs/` paths.
- `session_status.py` now reports `live_leases` from the central lease map so
  active service ports are visible even though the session creation record is
  static.

## Remaining High-Value Validation

- Full `remote-code-parity --apply-mode install` was intentionally not run in
  the two scratch sessions because it would replace image-provided editable
  packages and trigger remote rebuild/install work. `source-only` and
  `materialize` were validated against distinct session worktrees.
- `ascend-memory-profiling` was not run end-to-end because profiling collection
  already covered real profiler artifacts, and the memory-profiling collision
  risk is now covered by local run-dir regression tests.
