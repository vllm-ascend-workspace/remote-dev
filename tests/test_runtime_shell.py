"""Real Bash semantics of the runtime script/command boundary."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.job_ops import _job_command


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Remote command runs in Linux Bash")


def run(tmp_path, runtime, command, **env):
    profile = tmp_path / "runtime file.sh"
    profile.write_text(runtime)
    endpoint = Endpoint(host="example.invalid", port=22, runtime_env_file=str(profile))
    return subprocess.run(["bash", "-c", _job_command(endpoint, command, True)],
                          cwd=tmp_path, env={**os.environ, **env}, capture_output=True, text=True)


def test_runtime_functions_nonexported_variables_options_and_path_survive(tmp_path):
    result = run(tmp_path, """set -o pipefail
runtime_function() { printf '%s\\n' "$runtime_local"; }
runtime_local='runtime value'
export PATH='/runtime/first:'"$PATH"
""", 'runtime_function; printf "%s\\n" "$PATH"; false | true')
    assert result.stdout.startswith("runtime value\n/runtime/first:")
    assert result.returncode == 1


def test_bash_env_is_loaded_once_and_runtime_is_sourced_each_execution(tmp_path):
    startup = tmp_path / "startup.sh"
    startup.write_text('export STARTUP_COUNT=$(( ${STARTUP_COUNT:-0} + 1 ))\nstartup_fn() { echo startup; }\n')
    for expected in (1, 2):
        result = run(tmp_path, 'echo sourced >> runtime.log',
                     'startup_fn; printf "%s:%s:%s\\n" "$STARTUP_COUNT" "$PWD" "$EXPLICIT"',
                     BASH_ENV=str(startup), STARTUP_COUNT="0", EXPLICIT="override")
        assert result.returncode == 0
        assert result.stdout == f"startup\n1:{tmp_path}:override\n"
        assert (tmp_path / "runtime.log").read_text().splitlines() == ["sourced"] * expected


def test_failed_and_missing_runtime_never_execute_user_command(tmp_path):
    result = run(tmp_path, "return 37", "touch should-not-exist")
    assert result.returncode == 37
    endpoint = Endpoint(host="example.invalid", port=22, runtime_env_file=str(tmp_path / "missing"))
    missing = subprocess.run(["bash", "-c", _job_command(endpoint, "touch should-not-exist", True)], cwd=tmp_path)
    assert missing.returncode != 0
    assert not (tmp_path / "should-not-exist").exists()


def test_runtime_errexit_is_not_suppressed_by_wrapper(tmp_path):
    result = run(tmp_path, "set -e\nfalse\necho should-not-print", "echo should-not-run")
    assert result.returncode == 1
    assert result.stdout == ""


def test_exit_nounset_and_disabled_runtime_are_preserved(tmp_path):
    assert run(tmp_path, "true", "exit 23").returncode == 23
    assert run(tmp_path, "set -u", 'echo "$ABSENT_REMOTE_DEV_TEST_VARIABLE"').returncode != 0
    endpoint = Endpoint(host="example.invalid", port=22, runtime_env_file="missing")
    assert _job_command(endpoint, "echo plain", False) == "echo plain"
    assert _job_command(replace(endpoint, runtime_env_file=None), "echo plain", True) == "echo plain"
