"""Native OS process boundaries; the same tests execute on Windows/macOS/Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.local_process import OwnedProcess
from remote_dev.core import ssh_transport
from test_ssh_transport import _windows_pid_alive


TREE_SCRIPT = '''
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
role, parent_exit = sys.argv[2:]
if role == 'leaf':
    sys.stderr.buffer.write('leaf ready 中文\\n'.encode('utf-8')); sys.stderr.flush()
    (root / 'leaf.pid').write_text(str(os.getpid()))
elif role == 'branch':
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, __file__, str(root), 'leaf', parent_exit])
    while not (root / 'leaf.pid').exists(): time.sleep(.01)
    (root / 'branch.pid').write_text(str(os.getpid()))
else:
    subprocess.Popen([sys.executable, __file__, str(root), 'branch', parent_exit])
    while not (root / 'branch.pid').exists(): time.sleep(.01)
    (root / 'ready').write_text('ready')
    sys.stdout.buffer.write(b'parent ready\\n'); sys.stdout.flush()
    if parent_exit == 'yes': sys.exit(0)
time.sleep(12)
'''


def tree_command(tmp_path: Path, parent_exit: bool) -> list[str]:
    script = tmp_path / 'process tree.py'
    script.write_text(TREE_SCRIPT, encoding='utf-8')
    return [sys.executable, str(script), str(tmp_path), 'root', 'yes' if parent_exit else 'no']


def wait_ready(tmp_path: Path) -> None:
    deadline = time.monotonic() + 5
    while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (tmp_path / 'ready').exists(), 'process tree did not start'


def live(pid: int) -> bool:
    if os.name == 'nt':
        return _windows_pid_alive(pid)
    # A killed orphan can remain a zombie until the host init reaps it.
    # ps is available on both macOS and Linux; a zombie is not a live process.
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                           capture_output=True, text=True, check=False).stdout.strip()
    return bool(state and not state.startswith('Z'))


def assert_tree_stopped(tmp_path: Path) -> None:
    ids = [int((tmp_path / (name + '.pid')).read_text()) for name in ('branch', 'leaf')]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(live(pid) for pid in ids):
            return
        time.sleep(.02)
    assert not any(live(pid) for pid in ids), 'owned descendant survived stop'


def test_literal_argv_unicode_cwd_env_and_binary_pipes(tmp_path):
    original_env = os.environ.get('LOCAL_PROCESS_VALUE')
    cwd = tmp_path / "中文 path ' $dollar"
    cwd.mkdir()
    arguments = ["literal ' quote", '$not_expanded', '中文', 'semi;colon', 'back\\slash']
    payload = bytes(range(256)) * 2048
    code = (
        "import json,os,sys; "
        "print(json.dumps([os.getcwd(),sys.argv[1:],os.environ['LOCAL_PROCESS_VALUE']])); "
        "sys.stdout.flush(); data=sys.stdin.buffer.read(); "
        "sys.stderr.buffer.write(data[::-1]);sys.stderr.flush(); "
        "sys.stdout.buffer.write(data)"
    )
    with OwnedProcess([sys.executable, '-c', code, *arguments], cwd=cwd,
                      env={'LOCAL_PROCESS_VALUE': '值 $literal'}, stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as owner:
        stdout, stderr = owner.process.communicate(payload, timeout=5)
        assert owner.process.returncode == 0
    metadata, binary = stdout.split(b'\n', 1)
    actual_cwd, actual_args, actual_env = json.loads(metadata)
    assert Path(actual_cwd) == cwd
    assert actual_args == arguments
    assert actual_env == '值 $literal'
    assert binary == payload
    assert stderr == payload[::-1]
    assert os.environ.get('LOCAL_PROCESS_VALUE') == original_env


@pytest.mark.parametrize('parent_exit', [False, True])
def test_owned_stop_ends_grandchildren_and_inherited_pipes(tmp_path, parent_exit):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(15)'])
    try:
        with OwnedProcess(tree_command(tmp_path, parent_exit), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as owner:
            wait_ready(tmp_path)
            if parent_exit:
                assert owner.process.wait(timeout=3) == 0
            started = time.monotonic()
            owner.stop(force=False, timeout=.3)
            stdout, stderr = owner.process.communicate(timeout=2)
            assert time.monotonic() - started < 3
            assert b'parent ready' in stdout
            assert 'leaf ready 中文'.encode('utf-8') in stderr
            assert_tree_stopped(tmp_path)
            assert unrelated.poll() is None
            owner.stop()  # A repeated close cannot target a reused group id.
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize('parent_exit', [False, True])
def test_attached_stream_stops_tree_on_timeout_or_parent_exit(tmp_path, parent_exit):
    command = tree_command(tmp_path, parent_exit)
    started = time.monotonic()
    with mock.patch.object(ssh_transport, 'stream_ssh_command', return_value=command):
        result = ssh_transport.run_stream(Endpoint.for_long_stream('192.0.2.1', 22),
                                         '#' * 1000000, merge_stderr=False,
                                         timeout_ms=None if parent_exit else 1200)
    assert time.monotonic() - started < 4
    assert result.timed_out is (not parent_exit)
    assert result.returncode == (0 if parent_exit else None)
    assert result.stdout == 'parent ready\n'
    assert 'leaf ready 中文\n' in result.stderr
    assert_tree_stopped(tmp_path)


def test_forward_close_after_parent_exit_drains_child_pipe(tmp_path):
    with mock.patch.object(ssh_transport, 'local_forward_ssh_command',
                           return_value=tree_command(tmp_path, True)):
        forward = ssh_transport.open_local_forward(Endpoint.for_long_stream('192.0.2.1', 22),
                                                  8000, ready_timeout_s=None)
    try:
        wait_ready(tmp_path)
        assert forward._proc.wait(timeout=3) == 0
        started = time.monotonic()
        result = forward.close()
        assert time.monotonic() - started < 3
        assert result.returncode != 0
        assert 'leaf ready 中文\n' in result.stderr
        assert_tree_stopped(tmp_path)
    finally:
        forward.close()


def test_forward_startup_failure_stops_children_before_reading_stderr(tmp_path):
    with mock.patch.object(ssh_transport, 'local_forward_ssh_command',
                           return_value=tree_command(tmp_path, True)):
        started = time.monotonic()
        with pytest.raises(ssh_transport.RemoteExecutionError, match='exited early'):
            ssh_transport.open_local_forward(Endpoint.for_long_stream('192.0.2.1', 22),
                                             8000, ready_timeout_s=3)
    assert time.monotonic() - started < 4
    assert_tree_stopped(tmp_path)
