"""Atomic local publication, including transient Windows file sharing locks."""
from __future__ import annotations

import os
import time


def replace_file(source, destination):
    # A scanner/indexer may briefly deny delete sharing after a file read.
    # Retrying the same prepared replacement is safe; never delete the target.
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
