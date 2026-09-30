"""The on-demand consolidation (dreaming) engine.

Re-exports the public entry points: :func:`run_consolidation` (the pipeline
brain), :class:`ConsolidationResult` (its outcome), and
:class:`ConsolidationError` (the single error type the CLI translates to one
``error:`` line).
"""

from __future__ import annotations

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.pipeline import ConsolidationResult, run_consolidation

__all__ = ["ConsolidationError", "ConsolidationResult", "run_consolidation"]
