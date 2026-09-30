"""Exception type for the reindex surface.

Lifted into its own module (mirroring :mod:`palace.watch._errors`) so
:mod:`palace.reindex.cli`, :mod:`palace.reindex.observer`, and
:mod:`palace.reindex.server` all import the same base without forming an
import cycle. The CLI shim translates :class:`ReindexError` into a single
``error: <message>`` line on stderr with exit 1, matching the
``error:`` convention Phase 1.4 / 1.5 settled on for
:class:`palace.hooks._common.HooksError` and Phase 2.1 settled on for
:class:`palace.watch._errors.WatchError`.
"""

from __future__ import annotations

__all__ = ["ReindexError"]


class ReindexError(Exception):
    """Base exception for the reindex surface.

    Carries a single-line message; the ``palace reindex serve`` entry point
    translates this into ``error: ...`` on stderr and exits 1 without a
    Python traceback.
    """
