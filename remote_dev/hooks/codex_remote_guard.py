#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from remote_dev.hooks.guard_common import codex_response, inspect_payload, read_hook_payload


def main() -> int:
    decision = inspect_payload(read_hook_payload())
    sys.stdout.write(json.dumps(codex_response(decision), ensure_ascii=False) + "\n")
    return 2 if decision.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
