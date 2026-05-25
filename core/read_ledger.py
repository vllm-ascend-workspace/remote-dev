from __future__ import annotations

from pathlib import Path
from typing import Any

from .endpoint import Endpoint
from .state_store import load_read_ledger, read_ledger_path, write_read_ledger


def ledger_path(endpoint: Endpoint, file_path: str) -> Path:
    return read_ledger_path(endpoint, file_path)


def record_read(endpoint: Endpoint, file_info: dict[str, Any]) -> Path:
    return write_read_ledger(endpoint, file_info)


def load_read(endpoint: Endpoint, file_path: str) -> dict[str, Any] | None:
    return load_read_ledger(endpoint, file_path)
