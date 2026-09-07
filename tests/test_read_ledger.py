from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.path_policy import path_fingerprint  # noqa: E402
import core.state_store as state_store  # noqa: E402


class StateRootTests(unittest.TestCase):
    def test_state_root_defaults_inside_checkout_and_env_relocates_it(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REMOTE_DEV_STATE_DIR", None)
            self.assertEqual(state_store.state_root(), ROOT / "state")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": tmp}):
            self.assertEqual(state_store.state_root(), Path(tmp))
            endpoint = Endpoint(host="1.2.3.4", port=46000)
            base = state_store.ensure_endpoint_state(endpoint)
            self.assertTrue(str(base).startswith(tmp))
            self.assertTrue((base / "endpoint.json").exists())


class ReadLedgerTests(unittest.TestCase):
    def test_read_ledger_round_trip_uses_endpoint_state(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            original = state_store.substrate_root
            try:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                path = state_store.write_read_ledger(
                    endpoint,
                    {
                        "path": "/vllm-workspace/foo.py",
                        "sha256": "abc",
                        "size": 3,
                        "mtime_ns": 1,
                        "offset": 1,
                        "limit": 200,
                    },
                )
                self.assertTrue(path.exists())
                loaded = state_store.load_read_ledger(endpoint, "/vllm-workspace/foo.py")
                self.assertEqual(loaded["sha256"], "abc")
                self.assertIn(endpoint.endpoint_id, str(path))
            finally:
                state_store.substrate_root = original  # type: ignore[assignment]

    def test_read_ledger_scope_uses_uniform_encoding_for_every_nonempty_id(self) -> None:
        samples = (
            "safe",
            "agent/1",
            "agent_1",
            "agent_1-e23fba9d",
            "...",
            "-_-",
            "漢字",
            "a" * 81,
            "sess-" + "0" * 90,
            "default",
            "explicit",
        )
        scopes = []
        for raw in samples:
            scope = state_store.resolve_ledger_scope(raw)
            scopes.append(scope)
            self.assertEqual(scope, state_store.LEDGER_SCOPE_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest())
            self.assertRegex(scope, r"^id-[0-9a-f]{64}$")
            self.assertLessEqual(len(scope), 80)
            self.assertNotIn(scope, {".", "..", "/", state_store.LEDGER_NO_CONTEXT_SCOPE})
            self.assertNotEqual(scope, raw)
            self.assertNotEqual(scope, state_store.resolve_ledger_scope(scope))
        self.assertEqual(len(set(scopes)), len(samples))
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in state_store.LEDGER_SCOPE_ENV_VARS:
                os.environ.pop(name, None)
            self.assertEqual(state_store.resolve_ledger_scope(None), state_store.LEDGER_NO_CONTEXT_SCOPE)
            self.assertNotEqual(state_store.resolve_ledger_scope("default"), state_store.LEDGER_NO_CONTEXT_SCOPE)

    def test_read_ledger_scope_separates_sanitized_lookalikes_and_encoded_outputs(self) -> None:
        ctx_a = "agent/1"
        ctx_b = "agent_1-e23fba9d"
        self.assertEqual(state_store._legacy_disambiguated_scope(ctx_a), ctx_b)
        self.assertNotEqual(state_store.resolve_ledger_scope(ctx_a), state_store.resolve_ledger_scope("agent_1"))
        self.assertNotEqual(state_store.resolve_ledger_scope(ctx_a), state_store.resolve_ledger_scope(ctx_b))
        encoded_a = state_store.resolve_ledger_scope(ctx_a)
        self.assertNotEqual(encoded_a, state_store.resolve_ledger_scope(encoded_a))

    def test_read_ledger_scope_degrades_awkward_context_ids(self) -> None:
        for raw in ("a" * 81, "...", "-_-", "漢字"):
            scope = state_store.resolve_ledger_scope(raw)
            self.assertRegex(scope, r"^[A-Za-z0-9_.-]+$")
            self.assertLessEqual(len(scope), 80)
            self.assertNotIn(scope, {".", ".."})

    def test_read_ledger_scope_explicit_id_precedes_environment(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "from-env/value", "CODEX_SESSION_ID": "other"}, clear=False):
            env_scope = state_store.resolve_ledger_scope(None)
            self.assertEqual(env_scope, state_store.resolve_ledger_scope("from-env/value"))
            self.assertNotEqual(env_scope, state_store.LEDGER_NO_CONTEXT_SCOPE)
            explicit = state_store.resolve_ledger_scope("explicit")
            self.assertEqual(explicit, state_store.resolve_ledger_scope("explicit"))
            self.assertNotEqual(explicit, env_scope)

    def test_read_ledger_scope_isolated_by_client_context(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            original = state_store.substrate_root
            try:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                path = state_store.write_read_ledger(
                    endpoint,
                    {
                        "path": "/vllm-workspace/foo.py",
                        "sha256": "abc",
                        "size": 3,
                        "mtime_ns": 1,
                    },
                    client_context_id="context-a",
                )
                scope_a = state_store.resolve_ledger_scope("context-a")
                scope_b = state_store.resolve_ledger_scope("context-b")
                self.assertNotEqual(scope_a, scope_b)
                self.assertIn(f"/reads/{scope_a}/", path.as_posix())
                self.assertIsNone(state_store.load_read_ledger(endpoint, "/vllm-workspace/foo.py", client_context_id="context-b"))
                loaded = state_store.load_read_ledger(endpoint, "/vllm-workspace/foo.py", client_context_id="context-a")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["ledger_scope"], scope_a)
            finally:
                state_store.substrate_root = original  # type: ignore[assignment]

    def test_write_guard_requires_fresh_read_for_legacy_scope_without_using_sha(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        file_path = "/vllm-workspace/foo.py"
        with tempfile.TemporaryDirectory() as tmp:
            original = state_store.substrate_root
            try:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                legacy_scope = "agent_1-e23fba9d"
                legacy_path = (
                    state_store.ensure_endpoint_state(endpoint)
                    / "reads"
                    / legacy_scope
                    / f"{path_fingerprint(file_path)}.json"
                )
                state_store.atomic_write_json(
                    legacy_path,
                    {
                        "schema_version": "remote-dev.read_ledger.v1",
                        "endpoint_id": endpoint.endpoint_id,
                        "ledger_scope": legacy_scope,
                        "file_path": file_path,
                        "sha256": "legacy-shared-sha",
                        "size": 3,
                        "mtime_ns": 1,
                    },
                )
                guard = state_store.load_write_ledger_guard(endpoint, file_path, "agent/1")
                self.assertTrue(guard.read_required)
                self.assertIsNone(guard.ledger)
                self.assertEqual(guard.scope, state_store.resolve_ledger_scope("agent/1"))
                self.assertTrue(legacy_path.exists())
                self.assertIsNone(state_store.load_read_ledger(endpoint, file_path, "agent/1"))
                other = state_store.load_write_ledger_guard(endpoint, file_path, "unrelated-context")
                self.assertFalse(other.read_required)
                self.assertIsNone(other.ledger)
            finally:
                state_store.substrate_root = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
