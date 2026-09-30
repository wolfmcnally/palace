"""Exception types shared between :mod:`palace.watch.config` and the CLI shims.

The two surfaces need to import the same exception base without forming a cycle
(:mod:`palace.watch.cli` imports from :mod:`palace.watch.config`; both raise
errors the CLI translates into single-line stderr diagnostics with exit 1).
Lifting :class:`WatchError` and :class:`WatchRootsError` here keeps the
dependency direction flat.

The CLI-side translation mirrors the ``error: <message>`` shape Phase 1.4 / 1.5
settled on for :class:`palace.hooks._common.HooksError`. Operators see one
convention across ``palace capture``, ``palace hooks``, and ``palace watch``.
"""

from __future__ import annotations

__all__ = ["WatchError", "WatchRootsError"]


class WatchError(Exception):
    """Base exception for the watch surface.

    Carries a single-line message; CLI shims translate this into
    ``error: ...`` on stderr and exit 1 without a Python traceback.
    """


class WatchRootsError(WatchError):
    """Raised when a watch-roots config mutation precondition fails.

    Examples: adding a path that does not exist, adding a path that overlaps
    an existing watch root, removing a path that was never added.
    """
