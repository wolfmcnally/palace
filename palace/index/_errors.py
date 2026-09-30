"""Exception type for the index surface.

Lifted into its own module so :mod:`palace.index.cli`,
:mod:`palace.index.schema`, :mod:`palace.index.embedder`, and
:mod:`palace.index.server` all import the same base without forming an
import cycle. The CLI shim translates :class:`IndexError` into a single
``error: <message>`` line on stderr with exit 1, matching the
``error:`` convention Phase 1.4 / 1.5 settled on for
:class:`palace.hooks._common.HooksError`, Phase 2.1 for
:class:`palace.watch._errors.WatchError`, and Phase 2.2 for
:class:`palace.reindex._errors.ReindexError`.

This name intentionally shadows Python's builtin ``IndexError`` *only*
inside :mod:`palace.index`. Code in this subpackage never indexes into
out-of-bounds sequences in a way that would care; the shadow is the
explicit phase choice (see Phase 2.3 Open Questions / Minor Corrections).
"""

from __future__ import annotations

__all__ = ["IndexError", "StalePlanError"]


class IndexError(Exception):  # noqa: A001 — intentional shadow per phase
    """Base exception for the index surface.

    Carries a single-line message; the ``palace index serve`` entry point
    translates this into ``error: ...`` on stderr and exits 1 without a
    Python traceback.
    """


class StalePlanError(IndexError):
    """A file plan's prior state changed under it before its commit.

    Raised inside the commit transaction after the writer lock is held, so
    the caller re-plans against the store as it is now rather than committing
    rows derived from a prior state another writer has already replaced.
    """
