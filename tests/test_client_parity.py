"""Client-parity behavior: native-habit aliases, search flags, write append,
read-from-end, binary detection, and interactive job stdin.

The REMOTE_*_PY executors run through real local ``python3 -c`` subprocesses
(the same convention as test_grep_fallback.py), so the exact remote-side code
is exercised without SSH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.mcp.schemas import normalize_arguments  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402
import remote_dev.core.job_ops as job_ops  # noqa: E402
import remote_dev.core.search_ops as search_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
import remote_dev.mcp.tools as mcp_tools  # noqa: E402


def run_file_op(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", file_ops.REMOTE_FILE_PY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"file op helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_grep(payload: dict, *, path_env: str | None = None) -> dict:
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    proc = subprocess.run(
        [sys.executable, "-c", search_ops.REMOTE_SEARCH_PY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"grep helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class NormalizeArgumentsTests(unittest.TestCase):
    def test_read_aliases_fold_into_canonical_fields(self) -> None:
        args = normalize_arguments("remote.read", {"path": "/a/b.py", "line_offset": -50, "n_lines": 40})
        self.assertEqual(args, {"file_path": "/a/b.py", "offset": -50, "limit": 40})

    def test_canonical_key_wins_over_alias(self) -> None:
        args = normalize_arguments("remote.read", {"path": "/a.py", "file_path": "/b.py"})
        self.assertEqual(args["file_path"], "/b.py")
        self.assertNotIn("path", args)

    def test_grep_native_flags_and_unknown_keys(self) -> None:
        args = normalize_arguments(
            "remote.grep",
            {"pattern": "x", "-i": True, "-A": 2, "-B": 3, "-C": 1, "-n": False, "head_limit": 10, "output_mode": "count_matches", "lab": "gpu-1"},
        )
        self.assertEqual(args["case_insensitive"], True)
        self.assertEqual(args["after_context"], 2)
        self.assertEqual(args["before_context"], 3)
        self.assertEqual(args["context_lines"], 1)
        self.assertEqual(args["line_numbers"], False)
        self.assertEqual(args["limit"], 10)
        # count_matches is NOT folded into count: Kimi's mode counts total
        # matches per file (rg --count-matches), count counts matching lines.
        self.assertEqual(args["output_mode"], "count_matches")
        self.assertEqual(args["lab"], "gpu-1")
        for alias in ("-i", "-A", "-B", "-C", "-n", "head_limit"):
            self.assertNotIn(alias, args)

    def test_bash_codex_aliases(self) -> None:
        args = normalize_arguments("remote.bash", {"cmd": "ls", "workdir": "/srv/app"})
        self.assertEqual(args["command"], "ls")
        self.assertEqual(args["cwd"], "/srv/app")
        self.assertNotIn("cmd", args)
        self.assertNotIn("workdir", args)

    def test_tools_without_aliases_pass_through(self) -> None:
        args = {"host": "h", "port": 22, "foo": 1}
        self.assertEqual(normalize_arguments("remote.probe", args), args)

    def test_call_tool_applies_read_aliases(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_read", return_value={}) as execute:
            mcp_tools.call_tool("remote_read", {**endpoint, "path": "/x.py", "line_offset": -20, "n_lines": 10})
        self.assertEqual(execute.call_args.kwargs["file_path"], "/x.py")
        self.assertEqual(execute.call_args.kwargs["offset"], -20)
        self.assertEqual(execute.call_args.kwargs["limit"], 10)

    def test_aliased_required_fields_are_enforced_server_side(self) -> None:
        # file_path/command stay out of the wire schema's required list so
        # providers cannot reject alias-only calls; the server rejects a
        # genuinely missing field with an actionable message.
        from remote_dev.mcp.schemas import TOOL_SCHEMAS

        self.assertNotIn("file_path", TOOL_SCHEMAS["remote.read"].get("required", []))
        self.assertNotIn("command", TOOL_SCHEMAS["remote.bash"].get("required", []))
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_read") as execute:
            with self.assertRaisesRegex(ValueError, "file_path.*alias: path"):
                mcp_tools.call_tool("remote_read", endpoint)
            execute.assert_not_called()
        with mock.patch.object(mcp_tools, "remote_bash") as execute:
            with self.assertRaisesRegex(ValueError, "command.*alias: cmd"):
                mcp_tools.call_tool("remote_bash", endpoint)
            execute.assert_not_called()

    def test_call_tool_applies_bash_cmd_alias(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_bash", return_value={}) as execute:
            mcp_tools.call_tool("remote_bash", {**endpoint, "cmd": "ls -1", "workdir": "/srv"})
        self.assertEqual(execute.call_args.kwargs["command"], "ls -1")
        self.assertEqual(execute.call_args.kwargs["cwd"], "/srv")


class RemoteReadParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        (self.tree / "lines.txt").write_text("".join(f"line-{i}\n" for i in range(1, 11)), encoding="utf-8")

    def read(self, **overrides) -> dict:
        payload = {"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / "lines.txt")}
        payload.update(overrides)
        return run_file_op(payload)

    def test_negative_offset_reads_from_end(self) -> None:
        data = self.read(offset=-3, limit=2)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["line_start"], 8)
        self.assertIn("8 | line-8", data["file"]["content"])
        self.assertIn("9 | line-9", data["file"]["content"])

    def test_negative_offset_larger_than_file_clamps_to_start(self) -> None:
        data = self.read(offset=-500, limit=2)
        self.assertEqual(data["file"]["line_start"], 1)

    def test_zero_offset_folds_to_default_first_page(self) -> None:
        payload = {"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / "lines.txt"), "offset": 0}
        data = run_file_op(payload)
        self.assertIn(data["status"], {"ok", "partial"})
        self.assertEqual(data["file"]["line_start"], 1)

    def test_binary_file_returns_actionable_error(self) -> None:
        blob = self.tree / "blob.bin"
        blob.write_bytes(b"\x89PNG\x00\x0d\x0a")
        data = run_file_op({"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(blob)})
        self.assertEqual(data["status"], "binary_file")
        self.assertIn("remote.artifact_pull", data["error"])

    def test_utf8_text_with_replacement_chars_is_not_binary(self) -> None:
        text_file = self.tree / "gbk.txt"
        text_file.write_bytes("中文\n".encode("gbk"))
        data = run_file_op({"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(text_file)})
        self.assertIn(data["status"], {"ok", "partial"})


class RemoteWriteAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)

    def write(self, name: str, content: str, **flags) -> dict:
        payload = {"op": "write", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / name), "content": content}
        payload.update(flags)
        return run_file_op(payload)

    def test_append_extends_existing_file(self) -> None:
        self.write("a.txt", "first\n")
        data = self.write("a.txt", "second\n", append=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual(data["appended"], True)
        self.assertEqual((self.tree / "a.txt").read_text(encoding="utf-8"), "first\nsecond\n")

    def test_append_creates_missing_file(self) -> None:
        data = self.write("new.txt", "created\n", append=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual((self.tree / "new.txt").read_text(encoding="utf-8"), "created\n")

    def test_append_and_overwrite_are_rejected_together(self) -> None:
        self.write("a.txt", "first\n")
        data = self.write("a.txt", "x", append=True, overwrite=True)
        self.assertEqual(data["status"], "invalid_flags")

    def test_append_still_refuses_symlink(self) -> None:
        target = self.tree / "real.txt"
        target.write_text("real\n", encoding="utf-8")
        link = self.tree / "link.txt"
        link.symlink_to(target)
        data = self.write("link.txt", "x", append=True)
        self.assertEqual(data["status"], "symlink_not_allowed")


class RemoteGrepParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        (self.tree / "a.py").write_text("Alpha\nbeta\nGAMMA\ndelta\n", encoding="utf-8")
        (self.tree / "b.py").write_text("nothing here\n", encoding="utf-8")
        hidden = self.tree / ".hidden"
        hidden.mkdir()
        (hidden / "c.py").write_text("alpha in hidden\n", encoding="utf-8")

    def grep(self, path_env: str | None = None, **overrides) -> dict:
        payload = {"op": "grep", "root": str(self.tree), "cwd": str(self.tree), "path": str(self.tree), "pattern": "alpha"}
        payload.update(overrides)
        return run_grep(payload, path_env=path_env)

    def grep_fallback_path(self) -> str:
        # A PATH containing grep but not rg forces the POSIX fallback branch.
        grep = shutil.which("grep")
        assert grep, "grep must exist on this host"
        link_dir = self.tree / "bin"
        link_dir.mkdir(exist_ok=True)
        link = link_dir / "grep"
        if not link.exists():
            link.symlink_to(grep)
        return str(link_dir)

    def test_case_insensitive_content(self) -> None:
        for env in (None, self.grep_fallback_path()):
            data = self.grep(env, output_mode="content", case_insensitive=True)
            self.assertEqual(data["status"], "ok")
            joined = "\n".join(data["matches"])
            self.assertIn("Alpha", joined)

    def test_context_lines(self) -> None:
        for env in (None, self.grep_fallback_path()):
            data = self.grep(env, output_mode="content", pattern="beta", context_lines=1)
            joined = "\n".join(data["matches"])
            self.assertIn("Alpha", joined)
            self.assertIn("GAMMA", joined)

    def test_before_and_after_context(self) -> None:
        data = self.grep(None, output_mode="content", pattern="beta", before_context=1, after_context=0)
        joined = "\n".join(data["matches"])
        self.assertIn("Alpha", joined)
        self.assertNotIn("GAMMA", joined)

    def test_line_numbers_can_be_disabled(self) -> None:
        data = self.grep(None, output_mode="content", line_numbers=False)
        self.assertTrue(all("Alpha" in line and ":1:" not in line for line in data["matches"] if "a.py" in line))

    def test_offset_skips_result_lines(self) -> None:
        full = self.grep(None, output_mode="content", case_insensitive=True, pattern="a", limit=50)
        self.assertGreaterEqual(len(full["matches"]), 3)
        skipped = self.grep(None, output_mode="content", case_insensitive=True, pattern="a", offset=1, limit=50)
        self.assertEqual(skipped["matches"], full["matches"][1:])
        self.assertEqual(skipped["offset"], 1)

    def test_hidden_dirs_skipped_by_default_and_included_with_flag(self) -> None:
        default = self.grep(None, output_mode="content", case_insensitive=True, pattern="alpha in hidden")
        self.assertEqual(default["matches"], [])
        included = self.grep(None, output_mode="content", case_insensitive=True, pattern="alpha in hidden", include_ignored=True)
        self.assertTrue(any("c.py" in line for line in included["matches"]))
        fallback = self.grep(self.grep_fallback_path(), output_mode="content", case_insensitive=True, pattern="alpha in hidden", include_ignored=True)
        self.assertTrue(any("c.py" in line for line in fallback["matches"]))
        self.assertTrue(any("gitignore" in warning for warning in fallback["warnings"]))

    def test_count_and_count_matches_are_semantically_distinct(self) -> None:
        # Distinguishing input: one line holds two matches of the pattern.
        # count (rg -c) counts matching *lines*; count_matches (Kimi habit,
        # rg --count-matches) counts *matches*.
        multi = self.tree / "multi.txt"
        multi.write_text("aa\nbb\n", encoding="utf-8")
        for env in (None, self.grep_fallback_path()):
            lines = self.grep(env, output_mode="count", pattern="a", glob="multi.txt")
            matches = self.grep(env, output_mode="count_matches", pattern="a", glob="multi.txt")
            self.assertEqual(lines["matches"], [f"{multi}:1"], f"env={env}")
            self.assertEqual(matches["matches"], [f"{multi}:2"], f"env={env}")
            self.assertEqual(matches["total_matches"], 2, f"env={env}")
            self.assertIsNone(lines["total_matches"], f"env={env}")

    def test_new_flags_reach_remote_payload(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        captured = {}

        def fake_run(_endpoint, _script, payload, **_kwargs):
            captured.update(payload)
            return {"status": "ok", "matches": [], "truncated": False, "warnings": []}

        with mock.patch.object(search_ops, "run_remote_python", fake_run):
            search_ops.remote_grep(
                endpoint,
                pattern="x",
                case_insensitive=True,
                before_context=2,
                after_context=3,
                context_lines=1,
                line_numbers=False,
                include_ignored=True,
                offset=5,
            )
        self.assertEqual(captured["case_insensitive"], True)
        self.assertEqual(captured["before_context"], 2)
        self.assertEqual(captured["after_context"], 3)
        self.assertEqual(captured["context_lines"], 1)
        self.assertEqual(captured["line_numbers"], False)
        self.assertEqual(captured["include_ignored"], True)
        self.assertEqual(captured["offset"], 5)

    def test_context_on_non_content_mode_warns(self) -> None:
        data = self.grep(None, output_mode="files_with_matches", context_lines=2)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(any("context" in warning for warning in data["warnings"]))


class InteractiveJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.original_state_root = state_store.substrate_root
        state_store.substrate_root = lambda: self.tmp  # type: ignore[assignment]
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        state_store.substrate_root = self.original_state_root  # type: ignore[assignment]

    def test_interactive_spec_and_record(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        calls = []

        def fake_control(_endpoint, _job_id, action, **params):
            calls.append((action, params))
            return {"state": "prepared" if action == "prepare" else "running", "quiet": False, "gate_open": action == "go", "remote_dir": "/srv/.remote-dev/jobs/x"}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(endpoint, command="cat", job_id="job-interactive-spec", interactive=True)
        self.assertEqual(payload["result"]["status"], "running")
        self.assertTrue(calls[0][1]["spec"]["interactive"])
        record = state_store.read_json(Path(payload["result"]["refs"]["job_record"]))
        self.assertTrue(record["interactive"])
        self.assertEqual(payload["result"]["job"]["stdin_tool"], "remote.job_stdin")

    def test_yield_polls_until_quiet_and_returns_tail(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "prepare":
                return {"state": "prepared", "quiet": False, "gate_open": False, "remote_dir": "/srv/.remote-dev/jobs/y"}
            if action == "tail":
                return {"state": "succeeded", "quiet": True, "stdout": "done\n", "stderr": ""}
            return {"state": "succeeded", "quiet": True, "gate_open": True, "remote_dir": "/srv/.remote-dev/jobs/y"}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(endpoint, command="echo done", job_id="job-yield", yield_time_ms=5000)
        job = payload["result"]["job"]
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["yield"]["stdout"], "done\n")
        self.assertIn("state after yield: succeeded", payload["text"])

    def test_yield_time_is_clamped_with_warning(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "prepare":
                return {"state": "prepared", "quiet": False, "gate_open": False, "remote_dir": "/srv/r"}
            if action == "tail":
                return {"state": "running", "quiet": False, "stdout": "", "stderr": ""}
            return {"state": "running", "quiet": True, "gate_open": True, "remote_dir": "/srv/r"}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(endpoint, command="sleep 1", job_id="job-yield-clamp", yield_time_ms=10**9)
        self.assertTrue(any("clamped" in warning for warning in payload["result"]["warnings"]))
        self.assertEqual(payload["result"]["job"]["yield"]["yield_time_ms"], job_ops.MAX_YIELD_MS)

    def _write_record(self, endpoint: Endpoint, job_id: str, *, interactive: bool) -> None:
        state_store.atomic_write_json(
            state_store.job_record_path(endpoint, job_id),
            {"job_id": job_id, "target": endpoint.to_result_target(), "remote_dir": "/srv/.remote-dev/jobs/" + job_id, "interactive": interactive},
        )

    def test_job_stdin_rejects_non_interactive_job(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        self._write_record(endpoint, "job-plain", interactive=False)
        with mock.patch.object(job_ops, "control", side_effect=AssertionError("control must not run")):
            payload = job_ops.remote_job_stdin(None, job_id="job-plain", chars="x")
        self.assertEqual(payload["result"]["outcome"], "failed")
        self.assertEqual(payload["result"]["status"], "not_interactive")
        self.assertIn("interactive=true", payload["text"])

    def test_job_stdin_writes_and_returns_tail(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        self._write_record(endpoint, "job-inter", interactive=True)
        seen = {}

        def fake_control(_endpoint, _job_id, action, **params):
            seen.setdefault("actions", []).append(action)
            if action == "stdin":
                seen.update(params)
                return {"state": "running", "accepted": True, "written": len(params.get("data") or ""), "eof": params.get("eof", False)}
            if action == "tail":
                return {"state": "running", "quiet": False, "stdout": "got:x\n", "stderr": ""}
            return {"state": "running", "quiet": False}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.remote_job_stdin(None, job_id="job-inter", chars="x\n", yield_time_ms=100)
        self.assertEqual(payload["result"]["outcome"], "success")
        self.assertEqual(seen["data"], "x\n")
        self.assertEqual(seen["eof"], False)
        self.assertIn("got:x", payload["text"])
        self.assertEqual(payload["result"]["state"], "running")

    def test_job_stdin_eof_and_exit_code_surface(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        self._write_record(endpoint, "job-eof", interactive=True)

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "stdin":
                return {"state": "running", "accepted": True, "written": 2, "eof": True}
            if action == "tail":
                return {"state": "succeeded", "quiet": True, "stdout": "bye\n", "stderr": "", "result": {"state": "succeeded", "exit_code": 0}}
            return {"state": "succeeded", "quiet": True}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.remote_job_stdin(None, job_id="job-eof", chars="q\n", eof=True, yield_time_ms=100)
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertIn("exit code 0", payload["text"])
        self.assertIn("stdin closed", payload["text"])

    def test_job_stdin_rejected_by_supervisor_is_actionable(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        self._write_record(endpoint, "job-gone", interactive=True)

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "stdin":
                return {"state": "succeeded", "accepted": False, "written": 0, "reason": "job is succeeded, not running; stdin writes need a running job"}
            return {"state": "succeeded", "quiet": True}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.remote_job_stdin(None, job_id="job-gone", chars="x")
        self.assertEqual(payload["result"]["status"], "stdin_rejected")
        self.assertIn("not running", payload["text"])


class WorkerStdinActionTests(unittest.TestCase):
    """The worker's stdin control action is exercised locally (macOS-safe):
    job_status is stubbed, the FIFO and EOF marker are real filesystem objects.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        self.job_dir = self.tree / ".remote-dev" / "jobs" / "job-stdin-local"

    def _control(self, request):
        import remote_dev.processes.worker as worker_mod

        return worker_mod.control_job(request, worker_mod.__file__ and "")

    def test_stdin_write_reaches_fifo_and_eof_marks_file(self) -> None:
        import remote_dev.processes.worker as worker_mod

        root = self.tree
        (self.job_dir).mkdir(parents=True)
        request_base = {"root": str(root), "job_id": "job-stdin-local"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            # Directory exists but job was not prepared as interactive: no FIFO.
            reply = self._control({**request_base, "action": "stdin", "data": "x"})
            self.assertFalse(reply["accepted"])
            self.assertIn("interactive=true", reply["reason"])
            # Prepare the FIFO like an interactive prepare would.
            os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
            reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
            try:
                reply = self._control({**request_base, "action": "stdin", "data": "hello\n", "eof": True})
            finally:
                pass
            self.assertTrue(reply["accepted"])
            self.assertEqual(reply["written"], 6)
            self.assertTrue(reply["eof"])
            self.assertEqual(os.read(reader, 64), b"hello\n")
            os.close(reader)
            self.assertTrue((self.job_dir / "stdin-eof.json").exists())

    def test_stdin_rejected_when_not_running(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": "x"})
        self.assertFalse(reply["accepted"])
        self.assertIn("not running", reply["reason"])

    def test_stdin_empty_poll_on_terminal_job_is_accepted(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": ""})
        self.assertTrue(reply["accepted"])
        self.assertTrue(reply["polled_terminal"])

    def test_stdin_write_after_eof_is_rejected(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        worker_mod.atomic_json(self.job_dir / "stdin-eof.json", {"at": 0})
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": "more"})
        self.assertFalse(reply["accepted"])
        self.assertIn("already closed", reply["reason"])

    def test_stdin_large_write_reports_partial_without_loss_claim(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            big = "x" * (1024 * 1024)
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": big})
            self.assertTrue(reply["accepted"])
            self.assertLess(reply["written"], len(big))
            self.assertTrue(reply["stdin_buffer_full"])
            drained = os.read(reader, 2 * 1024 * 1024)
            self.assertEqual(drained, big[: reply["written"]].encode())
        finally:
            os.close(reader)

    def test_stdin_partial_write_defers_eof_and_accepts_retry(self) -> None:
        """Backpressure and EOF together: a partially accepted write with
        eof=true must not close stdin; the exact remainder stays retryable."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            big = "x" * (1024 * 1024)
            request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin"}
            drained = bytearray()
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                first = self._control({**request, "data": big, "eof": True})
                self.assertTrue(first["accepted"])
                self.assertLess(first["written"], len(big))
                self.assertTrue(first["stdin_buffer_full"])
                # EOF is deferred until every byte of this operation has been
                # accepted: no marker file, and the channel stays open.
                self.assertFalse(first["eof"])
                self.assertTrue(first["eof_deferred"])
                self.assertFalse((self.job_dir / "stdin-eof.json").exists())
                drained += os.read(reader, 2 * 1024 * 1024)
                remainder = big[first["written_chars"]:]
                row = first
                for _ in range(1000):
                    if not remainder:
                        break
                    row = self._control({**request, "data": remainder, "eof": True})
                    self.assertTrue(row["accepted"])
                    self.assertGreater(row["written"], 0)
                    drained += os.read(reader, 2 * 1024 * 1024)
                    remainder = remainder[row["written_chars"]:]
                else:
                    self.fail("stdin retry loop did not finish")
                self.assertFalse(row["stdin_buffer_full"])
                self.assertTrue(row["eof"])
            self.assertEqual(bytes(drained), big.encode())
            self.assertTrue((self.job_dir / "stdin-eof.json").exists())
        finally:
            os.close(reader)

    def test_stdin_unicode_write_reports_character_accounting(self) -> None:
        """written_chars is the exact character count of the accepted byte
        prefix; slicing the original string there never splits a character."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = "中文日志\n" * 5000  # 15 bytes per repetition
            request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin"}
            pending = data
            drained = bytearray()
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                for _ in range(1000):
                    row = self._control({**request, "data": pending, "eof": True})
                    self.assertTrue(row["accepted"])
                    self.assertGreater(row["written"], 0)
                    accepted = pending[: row["written_chars"]]
                    self.assertEqual(len(accepted.encode("utf-8")), row["written"])
                    drained += os.read(reader, 2 * 1024 * 1024)
                    pending = pending[row["written_chars"]:]
                    if not row["stdin_buffer_full"]:
                        break
                else:
                    self.fail("stdin retry loop did not finish")
                self.assertTrue(row["eof"])
            self.assertEqual(bytes(drained), data.encode("utf-8"))
        finally:
            os.close(reader)

    def test_tail_offset_mode_is_incremental(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        log = self.job_dir / "stdout.log"
        log.write_bytes(b"first\n")
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        row = self._control({**request, "stdout_offset": 0, "max_bytes": 4096})
        self.assertEqual(row["stdout"], "first\n")
        self.assertEqual(row["stdout_offset"], 6)
        self.assertEqual(row["stdout_bytes_remaining"], 0)
        log.write_bytes(b"first\nsecond\n")
        row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 4096})
        self.assertEqual(row["stdout"], "second\n")
        row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 4096})
        self.assertEqual(row["stdout"], "")

    def test_tail_incremental_preserves_split_utf8_characters(self) -> None:
        """A character split by the byte budget is held back for the next page
        instead of corrupting into U+FFFD; paged reads reassemble exactly."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        original = "中文日志" * 100
        (self.job_dir / "stdout.log").write_text(original, encoding="utf-8")
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            combined = ""
            cursor = 0
            for _ in range(20):
                row = self._control({**request, "stdout_offset": cursor, "max_bytes": 256})
                combined += row["stdout"]
                cursor = row["stdout_offset"]
                if row["stdout_bytes_remaining"] == 0:
                    break
            else:
                self.fail("paged incremental read did not finish")
        self.assertEqual(combined, original)
        self.assertNotIn("\ufffd", combined)
        self.assertEqual(cursor, len(original.encode("utf-8")))

    def test_tail_incremental_flushes_invalid_bytes_without_stalling(self) -> None:
        """Genuinely invalid bytes are not an incomplete sequence: they surface
        as U+FFFD and the cursor still advances past them."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        log = self.job_dir / "stdout.log"
        log.write_bytes("ok\n".encode() + b"\xff\xfe" + "end\n".encode())
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 4})
            self.assertEqual(row["stdout"], "ok\n\ufffd")
            self.assertEqual(row["stdout_offset"], 4)
            row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 256})
            self.assertEqual(row["stdout"], "\ufffdend\n")
            self.assertEqual(row["stdout_bytes_remaining"], 0)

    def test_tail_incremental_flushes_unfinished_tail_on_terminal_job(self) -> None:
        """A terminal job at end of file can never complete a trailing partial
        sequence, so it flushes as U+FFFD instead of staying held back."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        (self.job_dir / "stdout.log").write_bytes("tail\n".encode() + "中".encode("utf-8")[:2])
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 256})
        self.assertEqual(row["stdout"], "tail\n\ufffd")
        self.assertEqual(row["stdout_offset"], 7)
        self.assertEqual(row["stdout_bytes_remaining"], 0)

    def test_tail_incremental_tiny_budget_still_advances(self) -> None:
        """A budget smaller than one UTF-8 character must still move the cursor
        (progress guarantee); the partial bytes flush as U+FFFD."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        (self.job_dir / "stdout.log").write_bytes("中".encode("utf-8"))  # 3 bytes
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 2})
            self.assertEqual(row["stdout"], "\ufffd")
            self.assertEqual(row["stdout_offset"], 2)
            row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 2})
            self.assertEqual(row["stdout_offset"], 3)
            self.assertEqual(row["stdout_bytes_remaining"], 0)


class SessionSemanticsTests(unittest.TestCase):
    """Tool-level semantics: incremental polls, output budgets, PTY boundary."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.original_state_root = state_store.substrate_root
        state_store.substrate_root = lambda: self.tmp  # type: ignore[assignment]
        self.addCleanup(self._restore)
        self.endpoint = Endpoint(host="1.2.3.4", port=46000)

    def _restore(self) -> None:
        state_store.substrate_root = self.original_state_root  # type: ignore[assignment]

    def _write_record(self, job_id: str) -> None:
        state_store.atomic_write_json(
            state_store.job_record_path(self.endpoint, job_id),
            {"job_id": job_id, "target": self.endpoint.to_result_target(), "remote_dir": "/srv/.remote-dev/jobs/" + job_id, "interactive": True},
        )

    def test_repeated_polls_never_replay_output(self) -> None:
        self._write_record("job-poll")
        log = {"content": "line-1\n"}

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "stdin":
                return {"state": "running", "accepted": True, "written": len(params.get("data") or "")}
            if action == "tail":
                start = params.get("stdout_offset") or 0
                chunk = log["content"][start:]
                return {"state": "running", "quiet": False, "stdout": chunk, "stderr": "", "stdout_offset": len(log["content"]), "stdout_bytes_remaining": 0}
            return {"state": "running", "quiet": False}

        with mock.patch.object(job_ops, "control", fake_control):
            first = job_ops.remote_job_stdin(None, job_id="job-poll")
            self.assertIn("line-1", first["text"])
            second = job_ops.remote_job_stdin(None, job_id="job-poll")
            self.assertNotIn("line-1", second["text"])
            log["content"] += "line-2\n"
            third = job_ops.remote_job_stdin(None, job_id="job-poll")
            self.assertIn("line-2", third["text"])
            self.assertNotIn("line-1", third["text"])

    def test_initial_yield_initializes_cursors_and_poll_continues(self) -> None:
        """The initial yield uses the same incremental cursor path as
        job_stdin polls: it returns the first bytes up to the budget, persists
        stdin_cursors, and the follow-up poll delivers the skipped remainder
        without replaying the yielded prefix."""
        content = "first-output-" + "y" * 600 + "\n"

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "prepare":
                return {"state": "prepared", "quiet": False, "gate_open": False, "remote_dir": "/srv/.remote-dev/jobs/job-yield-cursor"}
            if action == "go":
                return {"state": "running", "quiet": False, "gate_open": True, "remote_dir": "/srv/.remote-dev/jobs/job-yield-cursor"}
            if action == "stdin":
                return {"state": "running", "accepted": True, "written": 0}
            if action == "tail":
                start = params.get("stdout_offset")
                self.assertIsNotNone(start, "initial yield must use the incremental offset tail, not the last-lines tail")
                budget = params["max_bytes"]
                chunk = content[start:start + budget]
                return {"state": "running", "quiet": False, "stdout": chunk, "stderr": "",
                        "stdout_offset": start + len(chunk),
                        "stdout_bytes_remaining": len(content) - start - len(chunk),
                        "stderr_offset": 0, "stderr_bytes_remaining": 0}
            return {"state": "running", "quiet": False, "remote_dir": "/srv/.remote-dev/jobs/job-yield-cursor"}

        with mock.patch.object(job_ops, "control", fake_control):
            started_payload = job_ops.start_remote_job(
                self.endpoint,
                command="generate",
                job_id="job-yield-cursor",
                interactive=True,
                yield_time_ms=100,
                max_output_tokens=64,  # 256-byte budget per stream
            )
            yield_info = started_payload["result"]["job"]["yield"]
            self.assertEqual(yield_info["stdout"], content[:256])
            self.assertEqual(yield_info["stdout_bytes_remaining"], len(content) - 256)
            record = state_store.read_json(Path(started_payload["result"]["refs"]["job_record"]))
            self.assertEqual(record["stdin_cursors"], {"stdout_offset": 256, "stderr_offset": 0})
            self.assertTrue(any("more byte(s) pending" in warning for warning in started_payload["result"]["warnings"]))
            poll = job_ops.remote_job_stdin(None, job_id="job-yield-cursor")
        self.assertEqual(poll["result"]["new_output"]["stdout"], content[256:])
        self.assertNotIn("first-output", poll["text"])

    def test_max_output_tokens_bounds_incremental_read_and_reports_remainder(self) -> None:
        self._write_record("job-budget")
        seen = {}

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "stdin":
                return {"state": "running", "accepted": True, "written": 0}
            if action == "tail":
                seen.update(params)
                return {"state": "running", "quiet": False, "stdout": "y" * 256, "stderr": "", "stdout_offset": 256, "stdout_bytes_remaining": 900}
            return {"state": "running", "quiet": False}

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.remote_job_stdin(None, job_id="job-budget", max_output_tokens=64)
        self.assertEqual(seen["max_bytes"], 256)  # 64 tokens * 4 chars
        self.assertTrue(any("900 more byte" in warning for warning in payload["result"]["warnings"]))
        self.assertEqual(payload["result"]["cursors"]["stdout_offset"], 256)

    def test_tty_returns_capability_boundary_without_connecting(self) -> None:
        import remote_dev.core.shell_ops as shell_ops

        with mock.patch.object(shell_ops, "run_script", side_effect=AssertionError("must not connect")), \
             mock.patch.object(job_ops, "control", side_effect=AssertionError("must not connect")):
            payload = shell_ops.remote_bash(self.endpoint, command="top", tty=True)
            payload_bg = shell_ops.remote_bash(self.endpoint, command="top", tty=True, run_in_background=True)
        for item in (payload, payload_bg):
            self.assertEqual(item["result"]["outcome"], "failed")
            self.assertEqual(item["result"]["status"], "unsupported_capability")
            self.assertIn("no PTY", item["text"])

    def test_foreground_max_output_tokens_caps_text(self) -> None:
        import remote_dev.core.shell_ops as shell_ops
        from remote_dev.core.ssh_transport import RemoteCompleted

        def fake_run_script(_endpoint, _script, **_kwargs):
            return RemoteCompleted(0, "z" * 10000 + "\n", "")

        with mock.patch.object(shell_ops, "run_script", fake_run_script):
            payload = shell_ops.remote_bash(self.endpoint, command="big", max_output_tokens=100)
        self.assertLessEqual(len(payload["text"]), 100 * 4 + 100)
        self.assertIn("capped by max_output_tokens", payload["text"])
        self.assertTrue(Path(payload["result"]["refs"]["stdout"]).exists())


if __name__ == "__main__":
    unittest.main()
