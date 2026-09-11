"""Request cancellation shared by protocol adapters and package operations."""
from __future__ import annotations

import contextlib
import contextvars
import threading

_event = contextvars.ContextVar("remote_dev_cancellation", default=None)


def current_event() -> threading.Event | None:
    return _event.get()


@contextlib.contextmanager
def request_context(event: threading.Event):
    token = _event.set(event)
    try:
        yield
    finally:
        _event.reset(token)
