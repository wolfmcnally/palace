"""Configuration constants and path resolvers for the consolidator.

Single source of truth for every tunable knob the on-demand dreaming engine
exposes: the local model identities, the six OpenClaw Deep Sleep score
weights, the three binding promotion gates, the contradiction-shortlist size,
the recency / frequency shaping parameters, and the canonical path resolvers
for the consolidator lock, ``dreams/DREAMS.md``, the per-day contradictions
file, and the day's session directory. ``policies/consolidation.md`` documents
the rationale for each value and cites ``policies/fact-schema.md`` §"Gates for
promotion" as the source of truth for the three gate thresholds; this module
restates them so the engine has one home for the literals.

The Ollama base URL and the embedding dimension are imported from
:mod:`palace.index.config` rather than re-declared — there is one home for the
on-device endpoint and the vector size.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

from palace.index.config import EMBED_DIM, OLLAMA_BASE_URL

__all__ = [
    "CAPTURE_FILENAME_RE",
    "CONTRADICTION_SHORTLIST_K",
    "DEFAULT_SOURCE_KIND",
    "EMBED_DIM",
    "EXTRACTION_MODEL",
    "FREQUENCY_SATURATION_COUNT",
    "GATE_CONFIDENCE",
    "GATE_CORROBORATION",
    "GATE_RELEVANCE",
    "NOVELTY_EMBED_MODEL",
    "OLLAMA_BASE_URL",
    "RECENCY_HALFLIFE_DAYS",
    "SCORING_MODEL",
    "SourceKind",
    "TRANSCRIPT_CHAR_BUDGET",
    "USER_TRUST_PREFIXES",
    "WEIGHTS",
    "WATERMARK_FILENAME",
    "consolidator_lock_path",
    "contradictions_path",
    "dreams_md_path",
    "sessions_dir",
    "watermark_path",
]


# ---------------------------------------------------------------- source kinds
#
# The consolidator's extraction source is pluggable in *shape*, not just path
# (``briefs/consolidator-extraction-source-pluggability.md``). ``sessions`` is
# Wolf's default — the day-partitioned captured session transcripts.
# ``captures`` is the external-consumer kind: a flat directory of pre-distilled
# Markdown capture files. The session-transcript reader stays the default; a
# captures run is gated behind an explicit ``--source-kind captures`` (or an
# inferred kind when ``--captures-root`` is supplied).
SourceKind = Literal["sessions", "captures"]
DEFAULT_SOURCE_KIND: SourceKind = "sessions"

# Per-kind source-trust (the "authored?" predicate; enhancement 3). For the
# captures kind, a capture counts as authored when its frontmatter ``source:``
# begins with one of these prefixes — a direct user statement (``user:*``)
# clears the corroboration-via-one-authored-source gate, while a system/agent
# source (``journal:*``, or a missing source) does not. Tunable knob; the gate
# *values* stay owned by ``policies/fact-schema.md`` §Gates.
USER_TRUST_PREFIXES: tuple[str, ...] = ("user:",)

# A capture file is named ``<slug>-<12hex>.md``; the 12-hex tail is the short
# form of the content-addressed id. Files not matching this shape are skipped
# (not errors) so an unrelated Markdown file in the inbox is ignored.
CAPTURE_FILENAME_RE: re.Pattern[str] = re.compile(r"^.+-[0-9a-f]{12}\.md$")

# The consolidation watermark filename (enhancement 2). Lives under the
# derived-index / lock directory (machine-local, gitignored), recording the
# id-set of captures already consolidated so a re-run does not re-extract them.
WATERMARK_FILENAME: str = "consolidate-watermark.json"


# ---------------------------------------------------------------- local models
#
# Extraction and scoring run on a local chat model; the novelty signal
# additionally measures candidate-vs-existing-fact similarity with the local
# embedding model. Both default on-device per ``policies/local-first.md``;
# cloud is the documented-quality fallback, not the default path. The model
# tags are verbatim Ollama tags and must match what ``ollama list`` reports.
#
# qwen3.6:35b-a3b is an A3B mixture-of-experts model (~3B active params/token):
# on Apple Silicon (M5 Max) it runs multiples faster than a dense 31B like
# gemma4:31b while matching or beating it on reasoning/calibration, which is the
# speed/quality balance the consolidator's cold-path batch wants. Ollama's
# XGrammar constrained decoding guarantees schema-valid JSON regardless of model.
EXTRACTION_MODEL: str = "qwen3.6:35b-a3b"
SCORING_MODEL: str = "qwen3.6:35b-a3b"
NOVELTY_EMBED_MODEL: str = "qwen3-embedding:8b"


# ---------------------------------------------------------------- score weights
#
# The six OpenClaw Deep Sleep weights. ``relevance`` (0.30) and ``frequency``
# (0.24) are the OpenClaw baseline; the residual 0.46 is split evenly across
# the remaining four signals (0.115 each). The set sums to exactly 1.0 — a
# test asserts this so a future re-tune cannot silently drift the total.
WEIGHTS: dict[str, float] = {
    "relevance": 0.30,
    "frequency": 0.24,
    "recency": 0.115,
    "importance": 0.115,
    "confidence": 0.115,
    "novelty": 0.115,
}


# ---------------------------------------------------------------- promotion gates
#
# The three binding promotion gates. ``policies/fact-schema.md`` §"Gates for
# promotion" is the source of truth; these literals restate it so the engine
# has one home. A candidate promotes only when it passes ALL three.
GATE_RELEVANCE: float = 0.5
GATE_CONFIDENCE: float = 0.6
GATE_CORROBORATION: int = 2


# ---------------------------------------------------------------- shaping knobs
#
# How many existing-fact neighbours the contradiction stage shortlists by
# embedding cosine before asking the chat model to confirm. Keeps the (slow)
# chat confirmation bounded to the most-similar candidates.
CONTRADICTION_SHORTLIST_K: int = 5

# Recency half-life in days: a candidate whose event_time is this many days
# before the run day scores 0.5 on recency; same-day scores 1.0.
RECENCY_HALFLIFE_DAYS: float = 30.0

# Source-count at which the frequency signal saturates to 1.0. A candidate
# corroborated by this many or more distinct source events scores 1.0; fewer
# scale linearly.
FREQUENCY_SATURATION_COUNT: int = 4

# Maximum chars of resolved transcript text fed to the extractor per session
# unit. Capture records store session metadata plus a ``transcript_path``
# pointer, not the inline dialogue; the consolidator resolves that pointer to
# the Claude Code transcript and renders it to turn-by-turn text. A real day's
# transcript can run to hundreds of KB — far past the extractor's useful
# context — so the rendering is capped to this budget with deterministic
# head+tail windowing (see ``palace/consolidate/sources.py``), preserving both
# the early task framing and the late conclusions.
TRANSCRIPT_CHAR_BUDGET: int = 16000


# ---------------------------------------------------------------- path resolvers


def consolidator_lock_path(store: Path) -> Path:
    """Return ``<store>/meta/consolidator.lock``, lazily creating ``meta/``.

    Mirrors the lazy-parent posture of :func:`palace.index.config.index_cursor_path`.
    """
    meta_dir = store / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    return meta_dir / "consolidator.lock"


def dreams_md_path(vault_root: Path) -> Path:
    """Return the canonical ``dreams/DREAMS.md`` path under ``vault_root``."""
    return vault_root / "dreams" / "DREAMS.md"


def contradictions_path(vault_root: Path, day: date) -> Path:
    """Return ``<vault-root>/dreams/contradictions/<YYYY-MM-DD>.md``."""
    return vault_root / "dreams" / "contradictions" / f"{day.isoformat()}.md"


def sessions_dir(store: Path, day: date) -> Path:
    """Return the canonical per-day session directory under ``store``.

    Does not create the directory: a day with no captured sessions has no
    such directory, and the reader treats absence as an empty day.
    """
    return store / "sessions" / day.isoformat()


def watermark_path(*, index_db: Path | None, lock_path: Path | None, store: Path) -> Path:
    """Resolve the consolidation watermark file, lazily creating its directory.

    The watermark is rebuildable machine-local state (an id-set of already-
    consolidated captures), so it lives under the **derived-index / lock**
    location — not the portable vault. Directory precedence (RESOLUTION 3):

    1. ``<--index-db parent>`` when an index-db is named;
    2. else ``<--lock-path parent>`` when a lock path is named;
    3. else ``<store>/meta/``.

    The chosen directory is created on demand so the first save succeeds.
    """
    if index_db is not None:
        directory = index_db.parent
    elif lock_path is not None:
        directory = lock_path.parent
    else:
        directory = store / "meta"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / WATERMARK_FILENAME
