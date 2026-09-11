"""Reusable binary-pipe SSH RPC, identical on Windows and POSIX clients.

Never retries a submitted request: a lost reply is an unknown outcome. A new
call can reconnect; persistent jobs remain discoverable by their job id.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
import contextlib
import hashlib
import json
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .cancellation import current_event
from .errors import RemoteExecutionError


class RpcConnection:
    def __init__(self, endpoint):
        from .ssh_transport import ssh_base_cmd
        source = (Path(__file__).parents[1] / "processes" / "rpc_worker.py").read_text(encoding="utf-8")
        helper = (Path(__file__).parents[1] / "processes" / "mutation.py").read_text(encoding="utf-8")
        source = source.replace("# REMOTE_DEV_MUTATION_LOCK", helper)
        transport_endpoint = replace(endpoint, ssh_mux=False, keepalive=True)
        self.proc = subprocess.Popen(
            [*ssh_base_cmd(transport_endpoint), "python3 -u -c " + shlex.quote(source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending = {}
        self.sent_codes = OrderedDict()
        self.sequence = 0
        self.closed = False
        self.error_tail = bytearray()
        self.ready = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.errors = threading.Thread(target=self._stderr, daemon=True)
        self.reader.start()
        self.errors.start()

    def _stderr(self):
        while True:
            chunk = self.proc.stderr.read(1024)
            if not chunk:
                return
            self.error_tail.extend(chunk)
            del self.error_tail[:-4000]

    def _read(self):
        try:
            for line in self.proc.stdout:
                value = json.loads(line.decode("utf-8"))
                if value.get("id") == 0 and value.get("ready"):
                    self.ready.set()
                    continue
                with self.state_lock:
                    waiter = self.pending.get(value.get("id"))
                if waiter is not None:
                    waiter.put(value)
        except (OSError, ValueError) as exc:
            self._fail(str(exc))
        finally:
            self._fail("SSH RPC disconnected; submitted operation outcome may be unknown")

    def _fail(self, reason):
        with self.state_lock:
            self.closed = True
            waiters = list(self.pending.values())
        self.ready.set()
        for waiter in waiters:
            waiter.put({"error": {"type": "RemoteExecutionError", "message": reason}})

    def _send(self, value):
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, kind, source, payload, timeout_ms):
        event = current_event()
        started = time.monotonic()
        reused = self.ready.is_set() and not self.closed
        startup_timeout = max(1, (timeout_ms or 45000) / 1000)
        while not self.ready.wait(0.05):
            if event is not None and event.is_set():
                raise RemoteExecutionError("SSH RPC cancelled before submission; request was not sent")
            if time.monotonic() - started >= startup_timeout:
                self.close()
                raise RemoteExecutionError("SSH RPC startup timed out; request was not sent")
        connected = time.monotonic()
        if event is not None and event.is_set():
            raise RemoteExecutionError("SSH RPC cancelled before submission; request was not sent")
        if self.closed:
            detail = self.error_tail.decode("utf-8", "replace").strip()
            raise RemoteExecutionError("SSH RPC unavailable; request was not sent" + (": " + detail if detail else ""))
        code_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
        waiter = queue.Queue()
        with self.write_lock:
            if self.closed:
                raise RemoteExecutionError("SSH RPC disconnected before submission; request was not sent")
            self.sequence += 1
            identifier = self.sequence
            with self.state_lock:
                self.pending[identifier] = waiter
            message = {"id": identifier, "kind": kind, "code_key": code_key,
                       "payload": payload, "timeout_ms": timeout_ms}
            if code_key not in self.sent_codes:
                message["code"] = source
            try:
                self._send(message)
                self.sent_codes[code_key] = None
                self.sent_codes.move_to_end(code_key)
                if len(self.sent_codes) > 32:
                    self.sent_codes.popitem(last=False)
            except (OSError, ValueError) as exc:
                with self.state_lock:
                    self.pending.pop(identifier, None)
                self.close()
                raise RemoteExecutionError("SSH RPC send failed; operation outcome may be unknown") from exc
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000 + (5 if kind == "python" else 0)
        event = current_event()
        cancel_sent = False
        try:
            while True:
                remaining = 60 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    with self.write_lock:
                        with contextlib.suppress(OSError, ValueError):
                            self._send({"kind": "cancel", "request_id": identifier})
                    raise RemoteExecutionError("SSH RPC request timed out; inspect the original job before retrying; outcome may be unknown")
                if event is not None and event.is_set() and not cancel_sent:
                    with self.write_lock:
                        self._send({"kind": "cancel", "request_id": identifier})
                    cancel_sent = True
                try:
                    value = waiter.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if "error" in value:
                    error = value["error"]
                    exception = {"ValueError": ValueError, "FileNotFoundError": FileNotFoundError,
                                 "NotADirectoryError": NotADirectoryError}.get(error.get("type"), RemoteExecutionError)
                    raise exception(error.get("message", "SSH RPC failed"))
                result = value["result"]
                if kind == "control" and isinstance(result, dict):
                    result["transport"] = {"connection_reused": reused,
                                           "connection_wait_ms": round((connected-started)*1000),
                                           "rpc_ms": round((time.monotonic()-connected)*1000)}
                return result
        finally:
            with self.state_lock:
                self.pending.pop(identifier, None)

    def close(self):
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._fail("SSH RPC connection closed")
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()


@dataclass
class _Entry:
    connection: object = None
    active: int = 0
    last_used: float = 0


_pool = {}
_pool_lock = threading.Condition()
_POOL_LIMIT = 32
_IDLE_SECONDS = 300
_reaper_started = False


def _idle_connections(now):
    expired = [key for key, entry in _pool.items()
               if entry.connection is not None and not entry.active
               and now - entry.last_used >= _IDLE_SECONDS]
    return [_pool.pop(key).connection for key in expired]


def _reap_idle():
    while True:
        with _pool_lock:
            _pool_lock.wait(timeout=min(60, _IDLE_SECONDS))
            connections = _idle_connections(time.monotonic())
            if connections:
                _pool_lock.notify_all()
        for connection in connections:
            connection.close()


def _acquire(endpoint, key, deadline):
    global _reaper_started
    event = current_event()
    while True:
        retired = None
        with _pool_lock:
            if event is not None and event.is_set():
                raise RemoteExecutionError("SSH RPC cancelled while waiting for a connection; request was not sent")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteExecutionError("SSH RPC connection capacity wait timed out; request was not sent")
            entry = _pool.get(key)
            if entry is not None and entry.connection is not None:
                connection = entry.connection
                if not connection.closed and connection.proc.poll() is None:
                    entry.active += 1
                    return entry
                # A failed transport cannot service its existing requests. They
                # retain their entry until finally; replacement never replays them.
                retired = _pool.pop(key).connection
                entry = None
            if entry is None:
                if len(_pool) >= _POOL_LIMIT:
                    idle = [(item.last_used, candidate) for candidate, item in _pool.items()
                            if item.connection is not None and not item.active]
                    if idle:
                        _, candidate = min(idle)
                        retired = _pool.pop(candidate).connection
                    else:
                        _pool_lock.wait(timeout=min(0.05, remaining))
                        continue
                entry = _Entry(active=1)
                _pool[key] = entry  # Reserve before opening, coalescing this key.
                if not _reaper_started:
                    threading.Thread(target=_reap_idle, daemon=True).start()
                    _reaper_started = True
            else:
                _pool_lock.wait(timeout=min(0.05, remaining))
                continue
        # Neither SSH process startup nor shutdown holds the global pool lock.
        try:
            if retired is not None:
                retired.close()
            connection = RpcConnection(endpoint)
        except BaseException:
            with _pool_lock:
                if _pool.get(key) is entry:
                    _pool.pop(key)
                _pool_lock.notify_all()
            raise
        with _pool_lock:
            registered = _pool.get(key) is entry
            if registered:
                entry.connection = connection
            _pool_lock.notify_all()
        if not registered:
            connection.close()
            raise RemoteExecutionError("SSH RPC pool closed before submission; request was not sent")
        return entry


def request(endpoint, kind, source, payload, *, timeout_ms=45000):
    # Include all connection and isolation inputs. In particular, two roots or
    # two identities on the same host do not silently borrow a connection.
    key = (endpoint.host, endpoint.port, endpoint.user, endpoint.identity_file,
           endpoint.root, endpoint.connect_timeout_ms)
    started = time.monotonic()
    entry = _acquire(endpoint, key, started + (timeout_ms or 45000) / 1000)
    acquired = time.monotonic()
    try:
        remaining_ms = None if timeout_ms is None else timeout_ms - int((acquired-started)*1000)
        if remaining_ms is not None and remaining_ms <= 0:
            raise RemoteExecutionError("SSH RPC connection wait timed out; request was not sent")
        result = entry.connection.request(kind, source, payload, remaining_ms)
        if kind == "control" and isinstance(result, dict):
            result["transport"]["pool_wait_ms"] = round((acquired-started)*1000)
        return result
    finally:
        with _pool_lock:
            entry.active -= 1
            entry.last_used = time.monotonic()
            _pool_lock.notify_all()


def close_connections():
    with _pool_lock:
        connections = [entry.connection for entry in _pool.values() if entry.connection is not None]
        _pool.clear()
        _pool_lock.notify_all()
    for connection in connections:
        connection.close()


atexit.register(close_connections)
