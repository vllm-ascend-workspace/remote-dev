"""Generic remote process supervision.

The public package boundary is :func:`remote_dev.processes.control`.
Ordinary background jobs and coordinator-managed executions use this
same implementation. The Linux worker is package data, not a coordinator
install.
"""

from .client import control, worker_source

__all__ = ["control", "worker_source"]
