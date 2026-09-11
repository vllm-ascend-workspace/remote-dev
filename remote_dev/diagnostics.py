"""Explicit connection diagnostics; never replay a caller's business command."""
from __future__ import annotations

import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace

from remote_dev.core.ssh_transport import _uses_shared_mux, run_script


def ssh_details(endpoint, timeout_ms=None) -> dict:
    return {"kind": "ssh", "mode": "multiplexed" if _uses_shared_mux(endpoint) else "independent",
            "connect_timeout_ms": max(1, endpoint.connect_timeout_ms // 1000) * 1000,
            "timeout_ms": timeout_ms}


def diagnose_ssh(endpoint, *, timeout_ms=10000) -> dict:
    if timeout_ms <= 0:
        raise ValueError("diagnostic timeout_ms must be positive")
    budget = min(5000, max(1, timeout_ms // 2))
    probes = []
    for target in (endpoint, replace(endpoint, ssh_mux=False)):
        if probes and (probes[0]["ok"] or not _uses_shared_mux(endpoint)):
            break
        result = run_script(target, "printf 'remote-dev-connection-ok\\n'", timeout_ms=budget)
        probes.append({**ssh_details(target, budget), "exit_code": result.returncode,
                       "timed_out": result.timed_out,
                       "ok": result.returncode == 0 and "remote-dev-connection-ok" in result.stdout,
                       "stderr": result.stderr[-1000:]})
    recovered = len(probes) == 2 and probes[1]["ok"]
    return {"probes": probes, "business_command_replayed": False,
            "status": "independent_connection_works" if recovered else "ok" if probes[0]["ok"] else "unavailable",
            "next": {"ssh_mux": False} if recovered else None}


def http_connection(request, *, proxy_mode="direct") -> dict:
    if proxy_mode not in {"direct", "environment"}:
        raise ValueError("proxy_mode must be direct or environment")
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    authority = host + (":" + str(parsed.port) if parsed.port else "")
    # Credentials and query parameters never enter diagnostics.
    return {"kind": "http", "target": urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", "")),
            "proxy_mode": proxy_mode}


def open_http(request, *, timeout, proxy_mode="direct"):
    http_connection(request, proxy_mode=proxy_mode)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}) if proxy_mode == "direct" else urllib.request.ProxyHandler())
    return opener.open(request, timeout=timeout)


def http_failure(exc: BaseException) -> dict:
    if isinstance(exc, urllib.error.HTTPError):
        return {"kind": "http_status", "status_code": exc.code}
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    kind = ("dns" if isinstance(cause, socket.gaierror) else
            "timeout" if isinstance(cause, (TimeoutError, socket.timeout)) else
            "connection_refused" if isinstance(cause, ConnectionRefusedError) else "connection")
    return {"kind": kind, "message": str(cause)[:300]}
