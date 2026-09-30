"""Configuration constants and path resolvers for the index daemon.

Single source of truth for the embedder model id, the Ollama base URL, the
output dimension, the Markdown chunking parameters, the ``FileKind``
Literal alias, and the canonical path resolvers under ``<store>/index/``
and ``<store>/meta/``. Tests import from here rather than re-declaring
their own constants so a constant change has one home.

Notes on naming:

- ``SCHEMA_TAG`` is the Python identifier; the literal string written to
  ``<store>/meta/schema-version`` is ``"chunks-v1"``. The constant is
  *not* named with a version suffix; the literal lives in a string and
  is exempt from the phase's greenfield grep gate per the
  Architecture-Decision disposition in the implementation plan.
- ``MARKDOWN_EXTENSIONS`` is restricted to ``.md`` in 2.3. The wider set
  (``.markdown`` etc.) is deferred to a future decision when Wolf's
  vault is surveyed; Phase 2.3 consistently uses ``.md`` throughout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from palace.writer_lock import WRITER_LOCK_TIMEOUT_SECONDS

__all__ = [
    "BINARY_EXTENSIONS",
    "CODE_EXTENSIONS",
    "EMBED_COSINE_TOLERANCE",
    "EMBED_CONCURRENCY_DEFAULT",
    "EMBED_DIM",
    "EMBED_INFLIGHT_MAX_CHUNKS",
    "EMBED_CONVENTION",
    "EMBEDDER_MODEL",
    "EmbeddingIdentity",
    "FileKind",
    "JSONL_EXTENSIONS",
    "MARKDOWN_EXTENSIONS",
    "MARKDOWN_HEADING_DEPTH",
    "MARKDOWN_MAX_SECTION_CHARS",
    "MARKDOWN_WINDOW_CHARS",
    "MARKDOWN_WINDOW_OVERLAP_CHARS",
    "OLLAMA_BASE_URL",
    "OPENROUTER_BASE_URL",
    "PARK_RECHECK_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "REMOTE_EMBED_MAX_BATCH",
    "REMOTE_EMBED_MAX_CONCURRENCY",
    "REMOTE_EMBED_MAX_ATTEMPTS",
    "REMOTE_EMBED_BACKOFF_BASE_SECONDS",
    "REMOTE_EMBED_BACKOFF_MAX_SECONDS",
    "REMOTE_EMBED_RETRY_STATUS_MIN",
    "SCHEMA_TAG",
    "TEXT_EXTENSIONS",
    "WRITER_READY_TIMEOUT_SECONDS",
    "WRITER_BUSY_TIMEOUT_SECONDS",
    "WRITER_LOCK_TIMEOUT_SECONDS",
    "MAX_STALE_REPLANS",
    "_BINARY_THRESHOLD",
    "_TEXT_DETECTOR_SAMPLE_BYTES",
    "chunks_db_path",
    "prepare_chunks_db_path",
    "index_cursor_path",
    "schema_version_path",
]


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """The complete identity shared by every vector in one store."""

    convention: str
    model: str
    provider: str
    dim: int


# Embedder identity. Switching the model requires a chunks-DB rebuild;
# ``index_meta.embed_model`` enforces the model axis of that identity.
EMBEDDER_MODEL: str = "qwen3-embedding:8b"

# Ollama base URL. ``OLLAMA_HOST`` overrides the default, matching the
# environment variable Ollama's own client honors.
OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# OpenRouter's index-time embedding API. Provider selection is store-level;
# this URL never selects the remote path by itself.
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# Output dimension for qwen3-embedding:8b. Pinned because the chunks-vec
# virtual table declares ``embedding FLOAT[4096]`` and a dim mismatch
# silently corrupts the dense index.
EMBED_DIM: int = 4096

# Empirical discrimination boundary (2026-08-08): intra-upstream and
# cross-upstream same-model jitter reached 9.9e-5. Local Ollama versus remote
# OpenRouter measured 7.7e-3 .. 1.19e-2 on the probe set and 2.15e-2 for the
# committed fixture's different text, an observed venue-gap span of
# 7.7e-3 .. 2.15e-2. This tolerance therefore distinguishes venues but cannot
# identify an upstream swap; the echoed provider check is the only
# upstream-drift detector.
EMBED_COSINE_TOLERANCE: float = 1e-3

# Maximum inputs palace puts in one *remote* embedding request. This is a
# provider limit, not a palace preference: DeepInfra — the pinned upstream —
# rejects a larger input list with HTTP 422 ("Value should have at most 1024
# items after validation, not 1137"), observed live 2026-08-08 against a
# 1137-section file. A larger list is split into ordered batches at this cap
# and each batch becomes its own audited request, so the audit trail stays
# one-record-per-request. Local Ollama documents no such limit and is not
# batched. Lower this if a future upstream caps inputs more tightly; raising it
# above an upstream's own limit only moves the 422 back to where it was.
REMOTE_EMBED_MAX_BATCH: int = 1024

# Highest concurrency level backed by a sustained measurement. N=8 held
# 25,768 tokens/s for two hours on 2026-08-16 without degradation; N=16 was
# faster in short bursts but was not sustained and remains an explicit-only
# operator choice.
EMBED_CONCURRENCY_DEFAULT: int = 8

# Highest remote concurrency level palace has measured. N=16 was the top of
# the 2026-08-16 burst curve; only N=8 has two-hour sustained evidence, so
# this is a capability ceiling rather than the policy default above.
REMOTE_EMBED_MAX_CONCURRENCY: int = 16

# Bound queued plans by vector count rather than file count because the
# measured corpus spans 5..1,721 chunks/file. At 4,096 dimensions and float32,
# 4,096 chunks are about 67 MB of vectors. One oversize file is admitted alone.
EMBED_INFLIGHT_MAX_CHUNKS: int = 4096

# Remote retries are bounded inside one request so a transient upstream shed
# does not discard a large file's completed first batches. HTTP 429 and the
# entire 5xx range are retryable; deterministic 4xx responses are not.
REMOTE_EMBED_MAX_ATTEMPTS: int = 4
REMOTE_EMBED_BACKOFF_BASE_SECONDS: float = 1.0
REMOTE_EMBED_BACKOFF_MAX_SECONDS: float = 30.0
REMOTE_EMBED_RETRY_STATUS_MIN: int = 500

# An identity-parked daemon holds no SQLite connection and rechecks only the
# four-row certificate. Writer startup itself has a bounded readiness wait; a
# timeout becomes an observable terminal park rather than an automatic retry.
PARK_RECHECK_SECONDS: float = 30.0
WRITER_READY_TIMEOUT_SECONDS: float = 30.0

# Every palace writer of the chunks DB serializes on the per-store writer lock
# (``palace.writer_lock``); the lock's one wait default lives there and is
# re-exported here for the index package. The SQLite busy timeout below covers
# what the lock does not: readers and foreign writers holding SQLite's own
# write lock for a moment.
WRITER_BUSY_TIMEOUT_SECONDS: float = 30.0

# A plan whose prior file state changed under it before its commit is planned
# again against the new state. Three consecutive stale attempts on one file
# mean another writer keeps changing it; the update then fails loudly rather
# than spinning.
MAX_STALE_REPLANS: int = 3


# Markdown chunking parameters. See the phase text for the rationale —
# the 4000-char cap is a retrieval-precision choice, not a model-capacity
# constraint (qwen3-embedding:8b accepts 32K tokens).
MARKDOWN_HEADING_DEPTH: int = 3
MARKDOWN_MAX_SECTION_CHARS: int = 4000
MARKDOWN_WINDOW_CHARS: int = 2000
MARKDOWN_WINDOW_OVERLAP_CHARS: int = 200


# How often the events-log tail polls for new data at EOF. 100 ms keeps
# steady-state latency tight without burning a measurable fraction of a
# CPU core.
POLL_INTERVAL_SECONDS: float = 0.1


# Schema-version marker. The literal on-disk string is ``"chunks-v1"``.
SCHEMA_TAG: str = "chunks-v1"

# Text convention used to build every vector in ``chunks_vec``. The marker
# lives inside the chunks DB so it travels with a shipped artifact. Bumping
# this value obsoletes every existing vector and requires a full rebuild; it
# does not change the DB schema or identify the embedding model.
EMBED_CONVENTION: str = "breadcrumb-path-heading-v1"


# The typed file-kind discriminator. ``"markdown"`` is the only kind 2.3
# actually indexes; the others are logged-and-skipped (Phase 2.4 lands
# the typed pipelines for jsonl / code / opaque text).
FileKind = Literal["markdown", "jsonl", "code", "text", "binary", "unknown"]


# File-extension sets used by :func:`palace.index.pipeline.classify_kind`.
# Restricted to ``.md`` in 2.3 by deliberate scope-creep removal —
# ``.markdown`` is deferred until Wolf's vault is surveyed.
MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md"})
JSONL_EXTENSIONS: frozenset[str] = frozenset({".jsonl"})
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
    }
)
TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".text", ".log", ".csv", ".tsv"})


# Suffixes the dispatcher refuses outright — no chunker handles binary
# bytes. The set is intentionally narrow: well-known container/archive
# formats, image/audio/video media, and OS-native compiled artifacts.
# Anything missing here that turns out to be binary still gets caught
# by the in-chunker non-printable-bytes heuristic (defense-in-depth).
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".7z",
        ".a",
        ".bin",
        ".bz2",
        ".class",
        ".dll",
        ".dmg",
        ".dylib",
        ".exe",
        ".gif",
        ".gz",
        ".heic",
        ".icns",
        ".ico",
        ".iso",
        ".jar",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".pdf",
        ".png",
        ".pyc",
        ".pyo",
        ".so",
        ".tar",
        ".tgz",
        ".wav",
        ".webp",
        ".xz",
        ".zip",
    }
)


# Binary-detection heuristic constants used by the opaque-text chunker
# (and re-used at the writer's defense-in-depth check). A file is
# classified binary when more than ``_BINARY_THRESHOLD`` of its first
# ``_TEXT_DETECTOR_SAMPLE_BYTES`` bytes fall outside the printable-ASCII
# range plus ``\t\n\r``, **unless** the sample decodes as valid UTF-8
# (a valid UTF-8 file with high-byte content is text, not binary).
_TEXT_DETECTOR_SAMPLE_BYTES: int = 4096
_BINARY_THRESHOLD: float = 0.10


def chunks_db_path(store: Path) -> Path:
    """Return the canonical chunks DB path without mutating a reader's store."""
    return store / "index" / "chunks.sqlite"


def prepare_chunks_db_path(store: Path) -> Path:
    """Create the database parent explicitly at a writer boundary."""
    result = chunks_db_path(store)
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def index_cursor_path(store: Path) -> Path:
    """Return the canonical index-cursor path under ``store``.

    Lazily creates ``<store>/meta/`` so the cursor's atomic-write idiom
    can replace into a guaranteed-existing parent directory.
    """
    meta_dir = store / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    return meta_dir / "index-cursor.json"


def schema_version_path(store: Path) -> Path:
    """Return the canonical schema-version marker path under ``store``.

    Lazily creates ``<store>/meta/``. The one-line plain-text file
    carries the literal string :data:`SCHEMA_TAG` (``"chunks-v1"``) once
    :func:`palace.index.schema.init_db` has bootstrapped the chunks DB.
    """
    meta_dir = store / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    return meta_dir / "schema-version"
