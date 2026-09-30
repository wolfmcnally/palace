"""Configuration constants and path resolvers for read-time retrieval.

This module is the single implementation home for the cross-encoder and
query-expansion knobs documented in ``policies/retrieval.md``.
"""

from __future__ import annotations

import os
from pathlib import Path

from palace.index.config import OLLAMA_BASE_URL

__all__ = [
    "EXPANSION_MODEL",
    "MULTI_QUERY_N",
    "OLLAMA_BASE_URL",
    "RERANK_ENABLED_DEFAULT",
    "RERANK_IN",
    "RERANK_LATENCY_BUDGET_MS",
    "RERANK_MAX_TOKENS",
    "RERANK_MODEL",
    "RERANK_OUT",
    "rerank_model_dir",
]


RERANK_MODEL: str = "ms-marco-MiniLM-L-6-v2"
RERANK_IN: int = 20
RERANK_OUT: int = 5

# Wolf ratified default-on before the personal eval on 2026-08-08, accepting
# the explicit extrapolation recorded in CLAUDE.md. Phase 8's first eval run
# confirms or reverses this one-constant decision.
RERANK_ENABLED_DEFAULT: bool = True

# Whole-pair cap: [CLS] query [SEP] passage [SEP]. Warm batch-20 session.run
# p50 on this machine measured 52.5 ms at padded seq 125, 103.7 ms at 191,
# and 275.3 ms at 512. At 128, a typical eight-token query leaves about 117
# WordPiece tokens (roughly 460 characters) for the passage. ``only_second``
# truncation guarantees only the passage loses tokens. Raising this cap
# requires re-running ./bin/palace-rerank-bench --assert-under-ms 100.
RERANK_MAX_TOKENS: int = 128
RERANK_LATENCY_BUDGET_MS: int = 100

EXPANSION_MODEL: str = "qwen3.6:35b-a3b"

# Used only as argparse's const= value for a bare ``--multi-query``. Library
# callers always thread their requested rewrite count explicitly.
MULTI_QUERY_N: int = 4


def rerank_model_dir() -> Path:
    """Resolve the shared reranker directory without creating it."""
    models_override = os.environ.get("PALACE_MODELS_DIR")
    if models_override:
        models_dir = Path(models_override).expanduser()
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        xdg_data_path = Path(xdg_data_home).expanduser() if xdg_data_home else None
        data_home = (
            xdg_data_path
            if xdg_data_path is not None and xdg_data_path.is_absolute()
            else Path.home() / ".local/share"
        )
        models_dir = data_home / "palace" / "models"
    return models_dir / RERANK_MODEL
