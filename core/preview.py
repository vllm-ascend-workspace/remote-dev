from __future__ import annotations

DEFAULT_HEAD_CHARS = 12000
DEFAULT_TAIL_CHARS = 12000


def text_preview(
    value: str,
    *,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> dict[str, object]:
    byte_count = len(value.encode("utf-8", errors="replace"))
    if len(value) <= head_chars + tail_chars:
        return {
            "text": value,
            "bytes": byte_count,
            "truncated": False,
            "head_chars": head_chars,
            "tail_chars": tail_chars,
        }
    return {
        "head": value[:head_chars],
        "tail": value[-tail_chars:],
        "bytes": byte_count,
        "truncated": True,
        "head_chars": head_chars,
        "tail_chars": tail_chars,
    }


def stdout_stderr_preview(stdout: str, stderr: str) -> dict[str, object]:
    return {
        "stdout": text_preview(stdout),
        "stderr": text_preview(stderr),
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
        "truncated": len(stdout) > DEFAULT_HEAD_CHARS + DEFAULT_TAIL_CHARS
        or len(stderr) > DEFAULT_HEAD_CHARS + DEFAULT_TAIL_CHARS,
        "head_chars": DEFAULT_HEAD_CHARS,
        "tail_chars": DEFAULT_TAIL_CHARS,
    }


def tail_text(value: str, limit: int = DEFAULT_TAIL_CHARS) -> str:
    return value if len(value) <= limit else value[-limit:]
