"""Example consumer resolver plugin for remote-dev.

Load it into the MCP server (or any CLI wrapper) with:

    REMOTE_DEV_RESOLVERS=/absolute/path/to/examples/resolver_plugin.py:setup

The plugin owns every piece of consumer knowledge - here a tiny JSON file
mapping a ``lab`` name to a host/port - and remote-dev only ever calls back
into it. Replace the lookup with whatever your environment has: a machine
inventory, a session registry, a worktree binding file, a coordinator API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.endpoint import EndpointError, register_resolver, resolver_setup

LAB_FILE_ENV = "EXAMPLE_LAB_FILE"


def _labs() -> dict:
    path = os.environ.get(LAB_FILE_ENV)
    if not path:
        return {}
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def resolve_lab(payload: dict):
    """Return an endpoint payload for ``lab``; decline anything else."""
    lab = payload.get("lab")
    if not lab:
        return None  # not ours: let remote-dev try the next resolver
    labs = _labs()
    if lab not in labs:
        raise EndpointError(f"unknown lab {lab!r}; configured labs: {sorted(labs) or 'none'}")
    entry = labs[lab]
    return {
        "host": entry["host"],
        "port": int(entry["port"]),
        "user": entry.get("user", "root"),
        "cwd": entry.get("cwd"),
        "runtime_env_file": entry.get("runtime_env_file"),
        "kind": "example-lab",
        "source": {"lab": lab},
    }


@resolver_setup
def setup() -> None:
    register_resolver(resolve_lab, name="example-labs", fields=("lab",), description="Resolve `lab` names from EXAMPLE_LAB_FILE.")
