"""Deterministic contextual text shared by index-time and read-time scorers.

Markdown and code chunks prepend their watch-root-relative path and stored
heading/symbol. JSONL and opaque-text chunks prepend only their relative path.
The body is always appended byte-for-byte. Any change to this output format
must bump ``EMBED_CONVENTION`` in :mod:`palace.index.config` and force a
``palace index build --full`` rebuild of every operated store.
"""

from __future__ import annotations

__all__ = ["scored_text"]


def scored_text(*, path: str, heading: str | None, body: str) -> str:
    """Return the breadcrumb-prefixed representation used by both scorers."""
    if heading is not None and heading.strip():
        return f"{path} > {heading}\n\n{body}"
    return f"{path}\n\n{body}"
