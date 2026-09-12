"""Optional remote runtime environment preamble.

Some remote images ship a profile script that must be sourced before user
commands (toolchain paths, accelerator SDK variables, ...). remote-dev does
not know which one: the consumer names it through ``runtime_env_file`` on the
endpoint payload, an endpoint alias, a resolver result, or the process-wide
``REMOTE_DEV_RUNTIME_ENV_FILE`` default. When no file is configured the
preamble is empty and ``runtime_env`` is a no-op. Initialization runs on every
command in its Bash process. Functions, variables and shell options remain
available to that command; a missing or failed script prevents execution.
"""
from __future__ import annotations

import shlex

from .endpoint import Endpoint


def runtime_env_lines(endpoint: Endpoint, enabled: bool) -> list[str]:
    if not enabled or not endpoint.runtime_env_file:
        return []
    path = shlex.quote(endpoint.runtime_env_file)
    # Do not source inside `if`/`||`: Bash would suppress errexit throughout the
    # script. Do not alter nounset or start a child Bash that loses functions.
    return [f". {path}", "__remote_dev_runtime_status=$?",
            'if [ "$__remote_dev_runtime_status" -ne 0 ]; then exit "$__remote_dev_runtime_status"; fi',
            "unset __remote_dev_runtime_status"]
