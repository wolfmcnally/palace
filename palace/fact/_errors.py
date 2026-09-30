"""Exception type for the fact surface.

Mirrors :mod:`palace.index._errors`: a single-line message that the
``palace fact`` CLI shim translates into one ``error: <message>`` line on
stderr with exit 1 — no Python traceback reaches the operator. Lifted into
its own module so :mod:`palace.fact.store`, :mod:`palace.fact.schema`, and
:mod:`palace.fact.cli` import the same base without forming an import cycle.
"""

from __future__ import annotations

__all__ = ["FactError"]


class FactError(Exception):
    """Base exception for the fact surface.

    Carries a single-line message; the ``palace fact`` entry point
    translates this into ``error: ...`` on stderr and exits 1 without a
    Python traceback.
    """
