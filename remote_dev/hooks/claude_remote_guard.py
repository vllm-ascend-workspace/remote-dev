#!/usr/bin/env python3
from __future__ import annotations

import sys

from remote_dev.hooks.guard_common import inspect_payload, read_hook_payload


def main() -> int:
    decision = inspect_payload(read_hook_payload())
    if decision.blocked:
        sys.stderr.write((decision.reason or "blocked by remote-dev guard") + "\n")
        return 2
    if decision.additional_context:
        sys.stdout.write(decision.additional_context + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
