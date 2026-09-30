"""Exception type for the consolidation surface.

Mirrors :mod:`palace.fact._errors` and :mod:`palace.index._errors`: a
single-line message that the ``palace consolidate`` CLI shim translates into
one ``error: <message>`` line on stderr with exit 1 — no Python traceback
reaches the operator. Lifted into its own module so every
:mod:`palace.consolidate` submodule imports the same base without forming an
import cycle.
"""

from __future__ import annotations

__all__ = ["ConsolidationError", "EmptySourceError"]


class ConsolidationError(Exception):
    """Base exception for the consolidation surface.

    Carries a single-line message; the ``palace consolidate`` entry point
    translates this into ``error: ...`` on stderr and exits 1 without a
    Python traceback.
    """


class EmptySourceError(ConsolidationError):
    """Raised when a selected episodic source resolves to zero source units.

    The loud empty-source guard (``briefs/consolidator-extraction-source-
    pluggability.md`` enhancement 4): a configured/selected source that reads
    *no* units is a hard, non-zero-exit condition — never a silent successful
    empty run — so an external consumer can honor a no-silent-lossy-fallback
    policy. Fires only on **zero reader units**, before any watermark filter; a
    captures run whose watermark drains every unit is a clean zero-promoted
    result, not this error.
    """
