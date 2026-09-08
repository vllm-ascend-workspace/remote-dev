"""Optional remote runtime environment preamble.

Some remote images ship a profile script that must be sourced before user
commands (toolchain paths, accelerator SDK variables, ...). remote-dev does
not know which one: the consumer names it through ``runtime_env_file`` on the
endpoint payload, an endpoint alias, a resolver result, or the process-wide
``REMOTE_DEV_RUNTIME_ENV_FILE`` default. When no file is configured the
preamble is empty and ``runtime_env`` is a no-op.
"""
from __future__ import annotations

import shlex

from .endpoint import Endpoint


def runtime_env_lines(endpoint: Endpoint, enabled: bool) -> list[str]:
    if not enabled or not endpoint.runtime_env_file:
        return []
    path = shlex.quote(endpoint.runtime_env_file)
    return [f"if [ -f {path} ]; then set +u; . {path}; set -u; fi"]
