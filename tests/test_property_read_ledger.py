"""Property tests for the read ledger and the stale-write guard.

The guard is optional by design: a write without a prior read in the same
client context is allowed. Within one context, however, the property is
strict — any interleaving in which the file changed after the context's last
read (and before its write) must be rejected with ``file_changed_since_read``,
and a write immediately after a read must succeed. The sequences below are
generated and checked against an explicit model of "last observed sha per
(context, file)".

The tests run the real ``core.file_ops`` entry points end to end with the
remote executor script executed in-process on a temporary tree, so the ledger
persisted locally and the sha check performed remotely are both exercised.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (ROOT, TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import core.file_ops as file_ops  # noqa: E402
import core.read_ledger as read_ledger  # noqa: E402
import core.state_store as state_store  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
from core.path_policy import join_under_root, path_fingerprint  # noqa: E402
from test_property_support import DOC_HOSTS, MULTIBYTE, Gen, run_cases, run_remote_script  # noqa: E402

CONTEXTS = ("ctx-a", "ctx-b", None)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LedgerHarness:
    """Runs file_ops against a temporary root with an in-process 'remote'."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / "root"
        self.root.mkdir()
        self.state = base / "state"
        patchers = [
            mock.patch.object(state_store, "substrate_root", return_value=self.state),
            mock.patch.object(file_ops, "run_remote_python", side_effect=lambda _endpoint, code, payload, **_kw: run_remote_script(code, payload)),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patcher in patchers:
            patcher.start()
            test.addCleanup(patcher.stop)
        for name in state_store.LEDGER_SCOPE_ENV_VARS:
            os.environ.pop(name, None)
        self.endpoint = Endpoint(host=DOC_HOSTS[0], port=46000, root=str(self.root), cwd=str(self.root))

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str, ctx: str | None, **kwargs: Any) -> dict[str, Any]:
        return file_ops.remote_read(self.endpoint, file_path=rel, client_context_id=ctx, **kwargs)["result"]

    def write(self, rel: str, ctx: str | None, content: str) -> dict[str, Any]:
        return file_ops.remote_write(self.endpoint, file_path=rel, content=content, overwrite=True, client_context_id=ctx)["result"]

    def edit(self, rel: str, ctx: str | None, old: str, new: str) -> dict[str, Any]:
        return file_ops.remote_edit(self.endpoint, file_path=rel, old_string=old, new_string=new, client_context_id=ctx)["result"]

    def multi_edit(self, rel: str, ctx: str | None, edits: list[dict[str, Any]]) -> dict[str, Any]:
        return file_ops.remote_multi_edit(self.endpoint, file_path=rel, edits=edits, client_context_id=ctx)["result"]


def unique_content(gen: Gen, tag: str, count: int) -> str:
    return "".join(f"{tag}{i:02d} {gen.text('abcxyz ' + MULTIBYTE, 0, 8)}\n" for i in range(count))


class StaleWriteGuardProperties(unittest.TestCase):
    def test_interleavings_are_judged_exactly_by_the_last_read_model(self) -> None:
        def body(gen: Gen, index: int) -> None:
            harness = LedgerHarness(self)
            files = [f"f{index}_{i}.py" for i in range(gen.integer(1, 2))]
            for i, rel in enumerate(files):
                harness.path(rel).write_text(unique_content(gen, f"init{i}L", gen.integer(1, 4)), encoding="utf-8")
            # model[(ctx, file)] = sha observed by ctx at its last read/write, or None.
            model: dict[tuple[str | None, str], str | None] = {}
            for step in range(gen.integer(4, 9)):
                rel = gen.choice(files)
                ctx = gen.choice(CONTEXTS)
                current = sha(harness.path(rel).read_bytes())
                action = gen.choice(("read", "read", "external", "write", "edit", "multi_edit"))
                if action == "read":
                    partial = gen.boolean(0.4)
                    kwargs = {"offset": gen.integer(1, 3), "limit": gen.integer(1, 2)} if partial else {}
                    result = harness.read(rel, ctx, **kwargs)
                    self.assertIn(result["status"], {"ok", "partial"}, result)
                    ledger = read_ledger.load_read(harness.endpoint, str(harness.path(rel)), ctx)
                    self.assertIsNotNone(ledger)
                    self.assertEqual(ledger["sha256"], current, "ledger must record the whole-file sha even for partial reads")
                    self.assertEqual(ledger["size"], harness.path(rel).stat().st_size)
                    model[(ctx, rel)] = current
                elif action == "external":
                    harness.path(rel).write_text(harness.path(rel).read_text(encoding="utf-8") + f"ext{step} drift\n", encoding="utf-8")
                else:
                    observed = model.get((ctx, rel))
                    stale = observed is not None and observed != current
                    before_bytes = harness.path(rel).read_bytes()
                    if action == "write":
                        new_content = unique_content(gen, f"w{step}L", gen.integer(1, 3))
                        result = harness.write(rel, ctx, new_content)
                        ok_status = "written"
                    else:
                        lines = before_bytes.decode("utf-8").splitlines(keepends=True)
                        old = gen.choice(lines)
                        new = f"e{step} {gen.word(0, 4)}\n"
                        new_content = before_bytes.decode("utf-8").replace(old, new, 1)
                        if action == "edit":
                            result = harness.edit(rel, ctx, old, new)
                        else:
                            result = harness.multi_edit(rel, ctx, [{"old_string": old, "new_string": new}])
                        ok_status = "edited"
                    if stale:
                        self.assertEqual(result["status"], "file_changed_since_read", f"step {step}: stale {action} by {ctx!r} was accepted: {result}")
                        self.assertEqual(result["outcome"], "blocked")
                        self.assertEqual(harness.path(rel).read_bytes(), before_bytes, "rejected write must not touch the file")
                        self.assertEqual(result["file"]["before_sha256"] if "before_sha256" in result.get("file", {}) else result.get("before_sha256", current), current)
                    else:
                        self.assertEqual(result["status"], ok_status, f"step {step}: fresh {action} by {ctx!r} was rejected: {result}")
                        self.assertEqual(harness.path(rel).read_text(encoding="utf-8"), new_content)
                        after = sha(harness.path(rel).read_bytes())
                        self.assertEqual(result["changed_files"][0]["after_sha256"], after)
                        self.assertEqual(result["changed_files"][0]["before_sha256"], current)
                        ledger = read_ledger.load_read(harness.endpoint, str(harness.path(rel)), ctx)
                        self.assertEqual(ledger["sha256"], after, "a successful write must refresh the ledger")
                        model[(ctx, rel)] = after

        run_cases(60, body, label="stale-write guard interleavings")

    def test_guard_cannot_be_bypassed_by_reading_a_different_file_or_context(self) -> None:
        def body(gen: Gen, index: int) -> None:
            harness = LedgerHarness(self)
            target = f"t{index}.py"
            decoy = f"d{index}.py"
            harness.path(target).write_text("v1\n", encoding="utf-8")
            harness.path(decoy).write_text("decoy\n", encoding="utf-8")
            ctx = gen.choice(("ctx-a", "ctx-b"))
            other = "ctx-b" if ctx == "ctx-a" else "ctx-a"
            harness.read(target, ctx)
            harness.path(target).write_text("v2 external\n", encoding="utf-8")
            # Attempts to refresh knowledge without re-reading the target in ctx:
            bypass = gen.choice(("read-decoy", "read-other-context", "nothing"))
            if bypass == "read-decoy":
                harness.read(decoy, ctx)
            elif bypass == "read-other-context":
                harness.read(target, other)
            result = harness.write(target, ctx, "v3 stale write\n")
            self.assertEqual(result["status"], "file_changed_since_read", f"bypass {bypass} succeeded: {result}")
            self.assertEqual(harness.path(target).read_text(encoding="utf-8"), "v2 external\n")
            # Re-reading the target in the same context is the only legitimate refresh.
            harness.read(target, ctx)
            self.assertEqual(harness.write(target, ctx, "v3\n")["status"], "written")

        run_cases(30, body, label="guard bypass attempts")

    def test_ledger_round_trip_for_arbitrary_paths_and_contexts(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            harness = LedgerHarness(self)
            rel = "/".join(gen.text("abc_" + MULTIBYTE, 1, 6) for _ in range(gen.integer(1, 3)))
            path = str(harness.root / rel)
            ctx = gen.choice((
                None,
                gen.text("abc019", 1, 1) + gen.text("abc/ .:" + MULTIBYTE, 0, 29),
                gen.text(".-_", 1, 8),
                gen.text(MULTIBYTE, 1, 4),
                "a" * gen.integer(81, 100),
            ))
            info = {"path": path, "sha256": gen.text("0123456789abcdef", 64, 64), "size": gen.integer(0, 10**6), "mtime_ns": gen.integer(0, 10**18), "offset": 1, "limit": 200}
            ledger_path = read_ledger.record_read(harness.endpoint, info, ctx)
            self.assertTrue(ledger_path.is_relative_to(harness.state), "ledger must live under the substrate state dir")
            loaded = read_ledger.load_read(harness.endpoint, path, ctx)
            self.assertIsNotNone(loaded)
            for key in ("sha256", "size", "mtime_ns"):
                self.assertEqual(loaded[key], info[key])
            self.assertEqual(loaded["file_path"], path)
            self.assertEqual(loaded["endpoint_id"], harness.endpoint.endpoint_id)
            self.assertEqual(read_ledger.ledger_path(harness.endpoint, path, ctx), ledger_path)
            other_endpoint = Endpoint(host=DOC_HOSTS[1], port=46000, root=str(harness.root))
            self.assertIsNone(read_ledger.load_read(other_endpoint, path, ctx), "ledgers must not leak across endpoints")

        run_cases(80, body, label="ledger round trip")


class LedgerScopeProperties(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in state_store.LEDGER_SCOPE_ENV_VARS:
            os.environ.pop(name, None)

    def test_scope_is_a_safe_single_path_segment(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            raw = gen.one_of(
                lambda: gen.text("abcXYZ019", 1, 1) + gen.text("abcXYZ019_.-/\\ :;\n\x00" + MULTIBYTE, 0, 39),
                lambda: gen.choice(("", "/a", "a/../b", "-a-", "_b_", ".c.", "a" * 80, "/" + "a" * 79, "...", "漢字", "a" * 81)),
            )
            scope = state_store.resolve_ledger_scope(raw)
            self.assertRegex(scope, r"^[A-Za-z0-9_.-]+$")
            self.assertNotIn(scope, {".", ".."})
            self.assertNotIn("/", scope)
            self.assertLessEqual(len(scope), 80)
            self.assertEqual(scope, state_store.resolve_ledger_scope(raw), "scope must be deterministic")
            if not raw:
                self.assertEqual(scope, state_store.LEDGER_NO_CONTEXT_SCOPE)
            else:
                self.assertRegex(scope, r"^id-[0-9a-f]{64}$")
                self.assertNotEqual(scope, raw)
                self.assertNotEqual(scope, state_store.resolve_ledger_scope(scope))

        run_cases(600, body, label="ledger scope safety")

    def test_environment_fallback_is_ordered_and_encoded(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            chosen = gen.subset(state_store.LEDGER_SCOPE_ENV_VARS)
            for name in state_store.LEDGER_SCOPE_ENV_VARS:
                os.environ.pop(name, None)
            for name in chosen:
                os.environ[name] = f"{name.lower()}/value"
            scope = state_store.resolve_ledger_scope(None)
            explicit = state_store.resolve_ledger_scope("explicit")
            self.assertRegex(explicit, r"^id-[0-9a-f]{64}$")
            if not chosen:
                self.assertEqual(scope, state_store.LEDGER_NO_CONTEXT_SCOPE)
                self.assertNotEqual(explicit, scope)
            else:
                first = next(name for name in state_store.LEDGER_SCOPE_ENV_VARS if name in chosen)
                raw = f"{first.lower()}/value"
                self.assertEqual(scope, state_store.resolve_ledger_scope(raw))
                self.assertNotEqual(scope, raw)
                self.assertNotEqual(explicit, scope, "explicit context wins over env")

        run_cases(64, body, label="ledger scope env fallback")

    def test_long_or_punctuation_only_context_ids_degrade_to_a_safe_scope(self) -> None:
        """Client context ids are not under our control. Hash every nonempty
        effective id so every file tool still gets a single safe path segment."""
        for raw in ("a" * 81, "...", "-_-", "漢字", "sess-" + "0" * 90):
            scope = state_store.resolve_ledger_scope(raw)
            self.assertRegex(scope, r"^id-[0-9a-f]{64}$")
            self.assertLessEqual(len(scope), 80)

    def test_distinct_effective_ids_map_to_distinct_scopes(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            raw_a = gen.one_of(
                lambda: gen.text("abcXYZ019_.-/:" + MULTIBYTE, 1, 24),
                lambda: gen.choice(("agent/1", "agent_1", "agent_1-e23fba9d", "default", "...", "-_-", "a" * 81, "")),
            )
            raw_b = gen.one_of(
                lambda: gen.text("abcXYZ019_.-/:" + MULTIBYTE, 1, 24),
                lambda: gen.choice(("agent/1", "agent_1", "agent_1-e23fba9d", "default", "explicit", "漢字")),
            )
            if raw_a:
                encoded = state_store.resolve_ledger_scope(raw_a)
                self.assertNotEqual(encoded, state_store.resolve_ledger_scope(encoded))
            effective_a = raw_a or None
            effective_b = raw_b or None
            scope_a = state_store.resolve_ledger_scope(effective_a)
            scope_b = state_store.resolve_ledger_scope(effective_b)
            if (raw_a or "") != (raw_b or ""):
                self.assertNotEqual(scope_a, scope_b, (raw_a, raw_b, scope_a, scope_b))
            else:
                self.assertEqual(scope_a, scope_b)

        run_cases(400, body, label="ledger scope injectivity")

    def test_distinct_contexts_do_not_share_one_ledger_scope(self) -> None:
        """Unsafe characters used to map to ``_`` without a digest, so
        ``agent/1`` and ``agent_1`` shared a ledger directory. Context B's
        fresh read then refreshed "A's" guard and A's stale write passed.
        Every nonempty id now uses a uniform hash encoding."""
        harness = LedgerHarness(self)
        harness.path("shared.py").write_text("v1\n", encoding="utf-8")
        harness.read("shared.py", "agent/1")
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        harness.read("shared.py", "agent_1")
        result = harness.write("shared.py", "agent/1", "v3 from a stale view\n")
        self.assertNotEqual(state_store.resolve_ledger_scope("agent/1"), state_store.resolve_ledger_scope("agent_1"))
        self.assertEqual(result["status"], "file_changed_since_read", result)
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")


def _plant_legacy_ledger(harness: LedgerHarness, rel: str, scope: str, digest: str) -> Path:
    file_path = join_under_root(harness.endpoint.root, harness.endpoint.effective_cwd, rel)
    legacy_path = (
        state_store.ensure_endpoint_state(harness.endpoint)
        / "reads"
        / scope
        / f"{path_fingerprint(file_path)}.json"
    )
    state_store.atomic_write_json(
        legacy_path,
        {
            "schema_version": "remote-dev.read_ledger.v1",
            "endpoint_id": harness.endpoint.endpoint_id,
            "ledger_scope": scope,
            "file_path": file_path,
            "sha256": digest,
            "size": 3,
            "mtime_ns": 1,
            "read_at": "2026-01-01T00:00:00Z",
        },
    )
    return legacy_path


class EncodedLookalikeIsolationTests(unittest.TestCase):
    """A raw id equal to another id's encoded namespace must not share a ledger."""

    def test_encoded_lookalike_cannot_refresh_stale_write_guard(self) -> None:
        ctx_a = "agent/1"
        ctx_b = "agent_1-e23fba9d"
        self.assertNotEqual(state_store.resolve_ledger_scope(ctx_a), state_store.resolve_ledger_scope(ctx_b))
        harness = LedgerHarness(self)
        harness.path("shared.py").write_text("v1\n", encoding="utf-8")
        harness.read("shared.py", ctx_a)
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        harness.read("shared.py", ctx_b)
        result = harness.write("shared.py", ctx_a, "v3 from stale view\n")
        self.assertEqual(result["status"], "file_changed_since_read", result)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
        harness.read("shared.py", ctx_a)
        self.assertEqual(harness.write("shared.py", ctx_a, "v3 after reread\n")["status"], "written")
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v3 after reread\n")

    def test_absent_context_and_explicit_default_are_distinct(self) -> None:
        harness = LedgerHarness(self)
        self.assertEqual(state_store.resolve_ledger_scope(None), state_store.LEDGER_NO_CONTEXT_SCOPE)
        self.assertNotEqual(state_store.resolve_ledger_scope("default"), state_store.LEDGER_NO_CONTEXT_SCOPE)
        harness.path("shared.py").write_text("v1\n", encoding="utf-8")
        harness.read("shared.py", None)
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        harness.read("shared.py", "default")
        result = harness.write("shared.py", None, "v3 from stale default\n")
        self.assertEqual(result["status"], "file_changed_since_read", result)
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
        harness.read("shared.py", None)
        self.assertEqual(harness.write("shared.py", None, "v3 after reread\n")["status"], "written")

    def test_current_encoded_scope_used_as_raw_id_stays_independent(self) -> None:
        ctx_a = "agent/1"
        ctx_b = state_store.resolve_ledger_scope(ctx_a)
        harness = LedgerHarness(self)
        harness.path("shared.py").write_text("v1\n", encoding="utf-8")
        harness.read("shared.py", ctx_a)
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        harness.read("shared.py", ctx_b)
        result = harness.write("shared.py", ctx_a, "v3 from stale view\n")
        self.assertEqual(result["status"], "file_changed_since_read", result)
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")

    def test_write_edit_and_multi_edit_keep_independent_ledgers(self) -> None:
        ctx_a = "agent/1"
        ctx_b = "agent_1-e23fba9d"
        for action in ("write", "edit", "multi_edit"):
            with self.subTest(action=action):
                harness = LedgerHarness(self)
                harness.path("shared.py").write_text("v1\n", encoding="utf-8")
                harness.read("shared.py", ctx_a)
                harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
                harness.read("shared.py", ctx_b)
                if action == "write":
                    blocked = harness.write("shared.py", ctx_a, "v3 from stale view\n")
                elif action == "edit":
                    blocked = harness.edit("shared.py", ctx_a, "v2 external\n", "v3 from stale view\n")
                else:
                    blocked = harness.multi_edit(
                        "shared.py", ctx_a, [{"old_string": "v2 external\n", "new_string": "v3 from stale view\n"}]
                    )
                self.assertEqual(blocked["status"], "file_changed_since_read", blocked)
                self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
                if action == "write":
                    allowed = harness.write("shared.py", ctx_b, "v3 from b\n")
                    ok_status = "written"
                elif action == "edit":
                    allowed = harness.edit("shared.py", ctx_b, "v2 external\n", "v3 from b\n")
                    ok_status = "edited"
                else:
                    allowed = harness.multi_edit(
                        "shared.py", ctx_b, [{"old_string": "v2 external\n", "new_string": "v3 from b\n"}]
                    )
                    ok_status = "edited"
                self.assertEqual(allowed["status"], ok_status, allowed)
                self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v3 from b\n")


class LegacyLedgerMigrationTests(unittest.TestCase):
    def test_legacy_disambiguated_ledger_requires_fresh_read_and_is_preserved(self) -> None:
        harness = LedgerHarness(self)
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        legacy_path = _plant_legacy_ledger(harness, "shared.py", "agent_1-e23fba9d", sha(b"v1\n"))
        result = harness.write("shared.py", "agent/1", "v3 from stale view\n")
        self.assertEqual(result["status"], "read_required", result)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
        self.assertTrue(legacy_path.exists(), "legacy ledgers must not be deleted")
        harness.read("shared.py", "agent/1")
        self.assertEqual(harness.write("shared.py", "agent/1", "v3 after reread\n")["status"], "written")
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v3 after reread\n")
        self.assertTrue(legacy_path.exists())
        current = read_ledger.load_read(
            harness.endpoint,
            join_under_root(harness.endpoint.root, harness.endpoint.effective_cwd, "shared.py"),
            "agent/1",
        )
        self.assertIsNotNone(current)
        self.assertEqual(current["ledger_scope"], state_store.resolve_ledger_scope("agent/1"))
        self.assertNotEqual(current["ledger_scope"], "agent_1-e23fba9d")

    def test_legacy_sanitized_ledger_requires_fresh_read_for_write_edit_and_multi_edit(self) -> None:
        for action in ("write", "edit", "multi_edit"):
            with self.subTest(action=action):
                harness = LedgerHarness(self)
                harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
                legacy_path = _plant_legacy_ledger(harness, "shared.py", "agent_1", sha(b"v1\n"))
                if action == "write":
                    result = harness.write("shared.py", "agent/1", "v3 from stale view\n")
                elif action == "edit":
                    result = harness.edit("shared.py", "agent/1", "v2 external\n", "v3 from stale view\n")
                else:
                    result = harness.multi_edit(
                        "shared.py", "agent/1", [{"old_string": "v2 external\n", "new_string": "v3 from stale view\n"}]
                    )
                self.assertEqual(result["status"], "read_required", result)
                self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
                self.assertTrue(legacy_path.exists())
                harness.read("shared.py", "agent/1")
                if action == "write":
                    ok = harness.write("shared.py", "agent/1", "v3 after reread\n")
                    ok_status = "written"
                elif action == "edit":
                    ok = harness.edit("shared.py", "agent/1", "v2 external\n", "v3 after reread\n")
                    ok_status = "edited"
                else:
                    ok = harness.multi_edit(
                        "shared.py", "agent/1", [{"old_string": "v2 external\n", "new_string": "v3 after reread\n"}]
                    )
                    ok_status = "edited"
                self.assertEqual(ok["status"], ok_status, ok)
                self.assertTrue(legacy_path.exists())

    def test_legacy_shared_sha_is_not_authorization_for_a_distinct_context(self) -> None:
        harness = LedgerHarness(self)
        harness.path("shared.py").write_text("v2 external\n", encoding="utf-8")
        legacy_path = _plant_legacy_ledger(harness, "shared.py", "agent_1-e23fba9d", sha(b"v2 external\n"))
        # The colliding raw id used to pass through onto this directory. Do not
        # treat that shared SHA as a fresh read for this distinct context.
        result = harness.write("shared.py", "agent_1-e23fba9d", "v3 stolen guard\n")
        self.assertEqual(result["status"], "read_required", result)
        self.assertEqual(harness.path("shared.py").read_text(encoding="utf-8"), "v2 external\n")
        self.assertTrue(legacy_path.exists())
        unrelated = harness.write("shared.py", "fresh-context", "v3 from new context\n")
        self.assertEqual(unrelated["status"], "written", unrelated)

    def test_new_file_without_prior_read_still_writes(self) -> None:
        harness = LedgerHarness(self)
        result = harness.write("brand-new.py", "agent/1", "created without read\n")
        self.assertEqual(result["status"], "written", result)
        self.assertEqual(harness.path("brand-new.py").read_text(encoding="utf-8"), "created without read\n")


if __name__ == "__main__":
    unittest.main()
