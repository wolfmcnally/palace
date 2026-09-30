"""Hybrid retrieval over the chunks DB, plus the recall-escalation tiers.

This module implements the three recall tiers of the escalation contract
(``briefs/sota-memory-and-recall.md`` §D.1a) — "use the cheapest tier
that answers; only escalate when it doesn't":

- **L1 — ``palace search "<query>"``** (:func:`search`): hybrid BM25 +
  exhaustive vector + RRF fusion returning a truncated ``snippet`` per hit, so
  a top-N result set stays cheap.
- **L2 — ``palace search expand <chunk_id>``** (:func:`expand`): widen a
  hit to the full Markdown section it was clipped from (optionally with
  the sibling sections of the same source file).
- **L3 — ``palace search transcript <session_id>``**
  (:func:`transcript`): bypass chunking entirely and return the raw
  captured session JSONL the Phase 1 capture daemon wrote to
  ``<store>/sessions/YYYY-MM-DD/<session_id>.jsonl``.

All three tiers leave canonical content and indexes unchanged; explicitly
selected remote inference appends request audit records. L1 also
exposes a three-layer composition API: :func:`search_expanded` retrieves with
optional HyDE/multi-query expansion, :func:`rerank_candidates` hydrates that
pool, and :func:`search_reranked` applies the selected reranker. Expansion
is independent of ranking. The CLI reranks by default; library entry points
remain explicit and :func:`search_reranked` raises on operational failure
unless its caller deliberately selects lenient degradation.

L1 (the hybrid search) reads ``<store>/index/chunks.sqlite`` (the index
daemon's output) and runs two parallel searches against the same query:

- **BM25** via FTS5 ``chunks_fts`` (porter stemming, unicode tokenization).
- **Exhaustive vector** via sqlite-vec ``chunks_vec`` against an Ollama-served
  ``qwen3-embedding:8b`` (:data:`palace.index.config.EMBED_DIM`-dim,
  cosine distance).

Results are fused via **Reciprocal Rank Fusion** (k=60): each
candidate's score is ``sum(1/(K + rank))`` across the two retrievers.
Top-N by fused score is returned. Candidates appearing in only one
retriever's pool still score (the other term is 0) — vector-only and
lexical-only hits both survive into the fused top-N, just with a
smaller score than consensus hits.

The module is read-only against the chunks DB; it never writes. The
embedder is constructed only when ``mode`` requires vector retrieval
(``hybrid`` or ``vector``); ``bm25`` mode skips Ollama entirely so a
lexical-only query works even when the embedder is down.

A store may instead select its published Amazon S3 Vectors index as the vector
leg (``<store>/meta/vector-backend.toml``; :mod:`palace.index.s3vectors`): the
query is embedded the same way, sent to the index its publication receipt names,
and the matches feed the same fusion and reranking. sqlite-vec stays the default.

Every vector leg first asserts the DB's embed-text convention. A stale or
unstamped store fails before any embedding call; BM25-only search, reranking a
BM25 pool, ``expand``, and ``transcript`` remain exempt because they read no
stored vector.

``chunks.path`` is stored watch-root-relative (Phase 3.2); search
resolves it against the stored ``chunks.watch_root`` so every
:class:`SearchHit` still surfaces a clickable absolute path for display.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

import sqlite_vec

from palace.daemons.capture.config import DEFAULT_STORE
from palace.index._errors import IndexError
from palace.index.config import chunks_db_path
from palace.index.embedder import Embedder, OllamaEmbedder
from palace.index.embedder_config import (
    assert_remote_corpus_boundary,
    identity_for,
    load_embedder_config,
    resolve_embedder,
)
from palace.index.s3vectors import (
    S3VectorsBackend,
    VectorMatch,
    VectorUsage,
    VectorUsageSummary,
    current_vector_usage,
    resolve_vector_backend,
    vector_usage_scope,
)
from palace.index.schema import assert_identity, assert_identity_readable
from palace.metadata import (
    MetadataError,
    MetadataFilter,
    matching_chunks,
    metadata_generation,
    normalize_filters,
    parse_filters,
)
from palace.query_expand import OllamaQueryExpander, QueryExpander
from palace.rerank import (
    RerankCandidate,
    Reranker,
    RerankStatus,
    RerankUnavailableError,
    _warn_rerank_failed,
    load_reranker,
    rerank,
)
from palace.rerank_config import (
    RerankConfig,
    RerankIdentity,
    construct_reranker,
    load_rerank_config,
    preflight_reranker,
)
from palace.retrieval_config import (
    MULTI_QUERY_N,
    RERANK_ENABLED_DEFAULT,
    RERANK_IN,
    RERANK_OUT,
)

_PALACE_STORE_ENV_VAR: str = "PALACE_STORE"

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_POOL",
    "RRF_K",
    "ExpandResult",
    "ExpandSection",
    "RerankedSearch",
    "SearchHit",
    "SearchMode",
    "SEARCH_ESCALATION_COMMANDS",
    "TranscriptResult",
    "build_escalation_parser",
    "build_subparser",
    "dispatch_search",
    "expand",
    "rerank_candidates",
    "search",
    "search_expanded",
    "search_reranked",
    "transcript",
]


SearchMode = Literal["hybrid", "bm25", "vector"]

DEFAULT_LIMIT: int = 10
"""Default number of fused results returned to the operator."""

DEFAULT_POOL: int = 50
"""Default candidate pool size per retriever before fusion.

A wider pool lets a low-ranked-but-still-relevant hit from one
retriever surface in the fused top-N when the other retriever also
weakly agrees. 50 is generous for a single-user vault; raise for
larger corpora.
"""

RRF_K: int = 60
"""Reciprocal-Rank-Fusion dampening constant.

Larger K narrows the gap between rank 1 and rank N (each successive
rank contributes ``1/(K+rank)``). 60 is the canonical default from
Cormack, Clarke, and Buettcher (2009).
"""


@dataclass(frozen=True)
class SearchHit:
    """One fused search result.

    Field semantics by mode:

    - ``bm25_rank`` / ``bm25_score`` are populated whenever the chunk
      was in the BM25 pool. SQLite's ``bm25(chunks_fts)`` returns a
      negative score — lower (more negative) is better.
    - ``vec_rank`` / ``vec_distance`` are populated whenever the chunk
      was in the vector pool. ``distance`` is cosine distance (lower
      is better).
    - ``rrf_score`` is always present, even when only one retriever
      contributed (the other term contributes 0).
    - ``rerank_rank`` / ``rerank_score`` are populated only after the
      cross-encoder stage; both are ``None`` on every un-reranked path.
    - ``snippet`` carries FTS5's ``snippet()`` rendering with ``<<...>>``
      highlights when the chunk was a BM25 hit; otherwise it's the
      first :data:`_BODY_PREVIEW_CHARS` characters of the body with
      newlines collapsed.
    """

    chunk_id: str
    path: str
    heading: str | None
    snippet: str
    rrf_score: float
    bm25_rank: int | None
    bm25_score: float | None
    vec_rank: int | None
    vec_distance: float | None
    rerank_rank: int | None = None
    rerank_score: float | None = None


@dataclass(frozen=True)
class RerankedSearch:
    """A reranked result set with explicit application or failure status."""

    hits: list[SearchHit]
    status: RerankStatus
    detail: str | None
    reranker_identity: RerankIdentity | None = None
    vector_usage: VectorUsageSummary | None = None


@dataclass(frozen=True)
class ExpandSection:
    """One sibling section returned alongside an :class:`ExpandResult`.

    The ``--neighbors N`` / ``--around`` widening returns the surrounding
    sections of the same source file, each as one of these (the target
    chunk itself is excluded — it is the :class:`ExpandResult` proper).
    """

    section_index: int
    window_index: int
    heading: str | None
    body: str


@dataclass(frozen=True)
class ExpandResult:
    """The L2 ``expand`` result: one chunk widened to its full section.

    ``path`` is the resolved absolute display path (watch-root-relative
    ``chunks.path`` joined with ``chunks.watch_root`` via
    :func:`_resolve_path`). ``body`` is the untruncated section body —
    strictly more than the L1 ``snippet`` for the same chunk. ``kind`` is
    the chunk's source kind, surfaced so callers can tell a Markdown
    section from a JSONL-derived chunk. ``neighbors`` is empty unless
    widening was requested.
    """

    chunk_id: str
    path: str
    heading: str | None
    kind: str
    body: str
    neighbors: list[ExpandSection]


@dataclass(frozen=True)
class TranscriptResult:
    """The L3 ``transcript`` result: one captured session's raw dialogue.

    ``paths`` lists the resolved absolute JSONL paths in date order (a
    session sharded across two dated directories contributes two paths).
    ``records`` is the parsed JSON per non-blank line, concatenated in
    date order; ``raw_lines`` is the verbatim JSONL (blank lines
    skipped), the literal bytes the capture daemon wrote.
    """

    session_id: str
    paths: list[str]
    records: list[dict[str, Any]]
    raw_lines: list[str]


_BODY_PREVIEW_CHARS: int = 200
_SNIPPET_TOKEN_BUDGET: int = 16


@contextmanager
def _tracked_vector_usage() -> Iterator[VectorUsage]:
    """Total this call's S3 Vectors requests; an enclosing scope counts them too.

    A raised :class:`IndexError` carries the requests spent before it as
    ``vector_usage`` whenever any were made.
    """
    usage = VectorUsage(parent=current_vector_usage())
    try:
        with vector_usage_scope(usage):
            yield usage
    except IndexError as exc:
        summary = usage.summary()
        if summary.requests and getattr(exc, "vector_usage", None) is None:
            exc.vector_usage = summary  # type: ignore[attr-defined]
        raise


def _served_usage(usage: VectorUsage) -> VectorUsageSummary | None:
    summary = usage.summary()
    return summary if summary.requests else None


@contextmanager
def _query_snapshot(
    *,
    store: Path,
    mode: SearchMode,
    embedder: Embedder | None,
    expander: QueryExpander | None = None,
    hyde: bool = False,
    multi_query: int | None = None,
    reranker_config: RerankConfig | None = None,
    filters: Sequence[MetadataFilter] = (),
) -> Iterator[
    tuple[sqlite3.Connection, Embedder | None, QueryExpander | None, S3VectorsBackend | None]
]:
    """Bind provider checks to the same database snapshot used for retrieval.

    The store's selected vector leg is resolved here too: ``None`` for sqlite-vec, or
    its published S3 Vectors index after the receipt and identity checks.
    """
    try:
        with ExitStack() as stack:
            conn = _open_chunks_db_ro(chunks_db_path(store))
            stack.callback(conn.close)
            conn.execute("BEGIN")
            if filters:
                metadata_generation(conn)
            watch_roots = [
                Path(row[0]) for row in conn.execute("SELECT DISTINCT watch_root FROM chunks")
            ]
            if reranker_config is not None:
                preflight_reranker(reranker_config, store=store, watch_roots=watch_roots)
            backend: S3VectorsBackend | None = None
            if mode != "bm25":
                config = load_embedder_config(store)
                identity = identity_for(config)
                assert_identity_readable(conn)
                assert_identity(conn, identity)
                assert_remote_corpus_boundary(config=config, store=store, watch_roots=watch_roots)
                backend = resolve_vector_backend(
                    store=store, identity=identity, watch_roots=watch_roots
                )
                if backend is not None and filters:
                    raise IndexError(
                        "metadata filters are not supported with the s3vectors vector backend"
                    )
                if embedder is None:
                    if config.provider == "ollama":
                        embedder = OllamaEmbedder()
                    else:
                        embedder, selected = resolve_embedder(store)
                        stack.callback(embedder.close)
                        if selected != identity:
                            raise IndexError("embedding provider changed during query preparation")
                    if config.provider == "ollama":
                        stack.callback(embedder.close)
            if (hyde or multi_query is not None) and expander is None:
                expander = OllamaQueryExpander()
                stack.callback(expander.close)
                expander.probe()
            yield conn, embedder, expander, backend
    except (sqlite3.Error, MetadataError) as exc:
        raise IndexError(f"query database read failed: {exc}") from exc


def search(
    *,
    query: str,
    store: Path,
    mode: SearchMode = "hybrid",
    limit: int = DEFAULT_LIMIT,
    pool: int = DEFAULT_POOL,
    rrf_k: int = RRF_K,
    embedder: Embedder | None = None,
    filters: Sequence[MetadataFilter] = (),
) -> list[SearchHit]:
    """Run a hybrid (or BM25-only or vector-only) search against the chunks DB.

    Returns up to ``limit`` :class:`SearchHit` records sorted by fused
    RRF score (descending). Empty list when no candidate from either
    retriever matches; the caller is responsible for the
    "no results found" message.

    Raises :class:`IndexError` for unreadable chunks DB, unreachable
    Ollama (when vector mode is needed), or any sqlite error.
    ``embedder`` is a test seam: production callers leave it ``None``
    and the function constructs an :class:`OllamaEmbedder`.
    """
    filters = _validated_metadata_filters(filters)
    if not query.strip():
        raise IndexError("search query is empty")
    if mode not in ("hybrid", "bm25", "vector"):
        raise IndexError(f"unknown search mode: {mode}")
    if limit <= 0 or pool <= 0:
        raise IndexError("limit and pool must be positive")

    db_path = chunks_db_path(store)
    if not db_path.is_file():
        if filters:
            raise IndexError("metadata unavailable: no chunks database")
        return []

    with (
        _tracked_vector_usage(),
        _query_snapshot(store=store, mode=mode, embedder=embedder, filters=filters) as (
            conn,
            selected,
            _,
            backend,
        ),
    ):
        return _search_conn(
            conn,
            query=query,
            mode=mode,
            limit=limit,
            pool=pool,
            rrf_k=rrf_k,
            embedder=selected,
            filters=filters,
            vector_backend=backend,
        )


def search_expanded(
    *,
    query: str,
    store: Path,
    mode: SearchMode = "hybrid",
    limit: int = DEFAULT_LIMIT,
    pool: int = DEFAULT_POOL,
    rrf_k: int = RRF_K,
    hyde: bool = False,
    multi_query: int | None = None,
    embedder: Embedder | None = None,
    expander: QueryExpander | None = None,
    filters: Sequence[MetadataFilter] = (),
) -> list[SearchHit]:
    """Retrieve with optional expansion and no cross-encoder ranking stage."""
    filters = _validated_metadata_filters(filters)
    _validate_retrieval(
        query=query,
        mode=mode,
        limit=limit,
        pool=pool,
        hyde=hyde,
        multi_query=multi_query,
    )
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        if filters:
            raise IndexError("metadata unavailable: no chunks database")
        return []

    with (
        _tracked_vector_usage(),
        _query_snapshot(
            store=store,
            mode=mode,
            embedder=embedder,
            expander=expander,
            filters=filters,
            hyde=hyde,
            multi_query=multi_query,
        ) as (conn, selected, expansion, backend),
    ):
        return _fuse_variants(
            conn,
            query=query,
            mode=mode,
            limit=limit,
            pool=pool,
            rrf_k=rrf_k,
            embedder=selected,
            expander=expansion,
            filters=filters,
            hyde=hyde,
            multi_query=multi_query,
            vector_backend=backend,
        )


def rerank_candidates(
    *,
    query: str,
    store: Path,
    rerank_in: int = RERANK_IN,
    mode: SearchMode = "hybrid",
    pool: int = DEFAULT_POOL,
    rrf_k: int = RRF_K,
    hyde: bool = False,
    multi_query: int | None = None,
    embedder: Embedder | None = None,
    expander: QueryExpander | None = None,
    _reranker_config: RerankConfig | None = None,
    filters: Sequence[MetadataFilter] = (),
) -> tuple[list[SearchHit], list[RerankCandidate]]:
    """Retrieve the fused pool and hydrate its stored candidate fields."""
    filters = _validated_metadata_filters(filters)
    if rerank_in <= 0:
        raise IndexError("rerank_in must be positive")
    _validate_retrieval(
        query=query,
        mode=mode,
        limit=rerank_in,
        pool=pool,
        hyde=hyde,
        multi_query=multi_query,
    )
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        if filters:
            raise IndexError("metadata unavailable: no chunks database")
        return [], []

    remote: dict[str, VectorMatch] = {}
    with (
        _tracked_vector_usage(),
        _query_snapshot(
            store=store,
            mode=mode,
            embedder=embedder,
            expander=expander,
            filters=filters,
            hyde=hyde,
            multi_query=multi_query,
            reranker_config=_reranker_config,
        ) as (conn, selected, expansion, backend),
    ):
        fused = _fuse_variants(
            conn,
            query=query,
            mode=mode,
            limit=rerank_in,
            pool=pool,
            rrf_k=rrf_k,
            embedder=selected,
            expander=expansion,
            filters=filters,
            hyde=hyde,
            multi_query=multi_query,
            vector_backend=backend,
            remote=remote,
        )
        return fused, _hydrate_candidates(conn, [hit.chunk_id for hit in fused], remote)


def search_reranked(
    *,
    query: str,
    store: Path,
    rerank_in: int = RERANK_IN,
    limit: int = RERANK_OUT,
    mode: SearchMode = "hybrid",
    pool: int = DEFAULT_POOL,
    rrf_k: int = RRF_K,
    hyde: bool = False,
    multi_query: int | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    expander: QueryExpander | None = None,
    strict: bool | None = True,
    filters: Sequence[MetadataFilter] = (),
) -> RerankedSearch:
    """Retrieve and rerank, optionally degrading operational failures to fused order."""
    filters = _validated_metadata_filters(filters)
    if limit <= 0:
        raise IndexError("limit and pool must be positive")
    if rerank_in <= 0:
        raise IndexError("rerank_in must be positive")
    _validate_retrieval(
        query=query,
        mode=mode,
        limit=rerank_in,
        pool=pool,
        hyde=hyde,
        multi_query=multi_query,
    )
    owned_reranker = reranker is None
    configuration = load_rerank_config(store) if owned_reranker else None
    selected_identity = configuration.identity if configuration is not None else None
    # None requests the CLI posture: private endpoints are strict; local stays lenient.
    effective_strict = (
        (configuration is not None and configuration.provider == "endpoint")
        if strict is None
        else strict
    )
    with _tracked_vector_usage() as usage:
        try:
            fused, candidates = rerank_candidates(
                query=query,
                store=store,
                rerank_in=rerank_in,
                mode=mode,
                pool=pool,
                rrf_k=rrf_k,
                hyde=hyde,
                multi_query=multi_query,
                embedder=embedder,
                expander=expander,
                _reranker_config=configuration,
                filters=filters,
            )
            if not fused:
                # An empty fused pool short-circuits before the model is loaded:
                # there is nothing to rerank, so nothing can fail. `applied` here
                # is documented intent (policies/retrieval.md §9), not evidence
                # that the cross-encoder was exercised — a caller probing reranker
                # health needs a query that actually matches.
                return RerankedSearch(
                    hits=[],
                    status="applied",
                    detail=None,
                    reranker_identity=selected_identity,
                    vector_usage=_served_usage(usage),
                )
            try:
                if not candidates:
                    raise RerankUnavailableError(
                        "reranker candidate hydration returned no candidates"
                    )
                if owned_reranker:
                    assert configuration is not None
                    reranker = construct_reranker(
                        configuration, store=store, local_loader=load_reranker
                    )
                assert reranker is not None
                ranked = rerank(query=query, candidates=candidates, top_k=limit, reranker=reranker)
            except RerankUnavailableError as exc:
                if effective_strict:
                    raise
                detail = str(exc)
                _warn_rerank_failed(
                    detail=detail,
                    private=configuration is not None and configuration.provider == "endpoint",
                )
                return RerankedSearch(
                    hits=fused[:limit],
                    status="failed",
                    detail=detail,
                    reranker_identity=selected_identity,
                    vector_usage=_served_usage(usage),
                )

            hit_by_id = {hit.chunk_id: hit for hit in fused}
            hits = [
                replace(
                    hit_by_id[item.candidate.chunk_id],
                    rerank_rank=item.rank,
                    rerank_score=item.score,
                )
                for item in ranked
            ]
            return RerankedSearch(
                hits=hits,
                status="applied",
                detail=None,
                reranker_identity=selected_identity,
                vector_usage=_served_usage(usage),
            )
        finally:
            if owned_reranker and reranker is not None:
                reranker.close()


def expand(*, chunk_id: str, store: Path, neighbors: int = 0) -> ExpandResult:
    """L2 — widen a search hit to its full Markdown section.

    Reads ``<store>/index/chunks.sqlite`` read-only and returns the
    untruncated ``body`` for ``chunk_id``, plus the resolved absolute
    display ``path``, the ``heading``, and the chunk ``kind``. Works
    uniformly across all chunk kinds (the full body is returned
    regardless of kind).

    When ``neighbors > 0``, also returns the sibling sections of the same
    source file with ``section_index`` within ±``neighbors`` of the
    target's (the target chunk itself excluded), ordered by
    ``(section_index, window_index)``.

    Raises :class:`IndexError` when the chunks DB is missing or unreadable
    or ``chunk_id`` is unknown.
    """
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        raise IndexError(f"unknown chunk_id: {chunk_id}")

    conn = _open_chunks_db_ro(db_path)
    try:
        row = conn.execute(
            "SELECT path, watch_root, heading, kind, body, section_index, window_index "
            "FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise IndexError(f"unknown chunk_id: {chunk_id}")
        path, watch_root, heading, kind, body, section_index, _window_index = row
        display_path = _resolve_path(watch_root, path) if watch_root else path

        sibling_sections: list[ExpandSection] = []
        if neighbors > 0:
            sibling_rows = conn.execute(
                "SELECT section_index, window_index, heading, body "
                "FROM chunks "
                "WHERE watch_root = ? AND path = ? "
                "AND section_index BETWEEN ? AND ? "
                "AND chunk_id != ? "
                "ORDER BY section_index, window_index",
                (
                    watch_root,
                    path,
                    section_index - neighbors,
                    section_index + neighbors,
                    chunk_id,
                ),
            ).fetchall()
            sibling_sections = [
                ExpandSection(
                    section_index=s_index,
                    window_index=w_index,
                    heading=s_heading,
                    body=s_body,
                )
                for s_index, w_index, s_heading, s_body in sibling_rows
            ]
    finally:
        conn.close()

    return ExpandResult(
        chunk_id=chunk_id,
        path=display_path,
        heading=heading,
        kind=kind,
        body=body,
        neighbors=sibling_sections,
    )


def transcript(*, session_id: str, store: Path) -> TranscriptResult:
    """L3 — return a captured session's raw JSONL transcript.

    Resolves a bare ``session_id`` to its dated path(s) by scanning
    ``<store>/sessions/*/<session_id>.jsonl`` (the date lives in the
    directory name, not the id). A session sharded across two dated
    directories is concatenated in date order (sorted by directory name).

    ``raw_lines`` is the verbatim JSONL (blank lines skipped); ``records``
    is the parsed JSON per line. Read-only against the store.

    Raises :class:`IndexError` for a traversal-unsafe ``session_id`` or
    when no captured session matches.
    """
    safe_id = _safe_session_id(session_id)
    matches = sorted(
        (store / "sessions").glob(f"*/{safe_id}.jsonl"),
        key=lambda p: p.parent.name,
    )
    if not matches:
        raise IndexError(f"no captured session: {session_id}")

    raw_lines: list[str] = []
    records: list[dict[str, Any]] = []
    for match in matches:
        for line in match.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw_lines.append(line)
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IndexError(f"corrupt session record in {match.name}: {exc}") from exc
            if isinstance(parsed, dict):
                records.append(parsed)

    return TranscriptResult(
        session_id=safe_id,
        paths=[str(m) for m in matches],
        records=records,
        raw_lines=raw_lines,
    )


# --------------------------------------------------------------------- internals


def _safe_session_id(session_id: str) -> str:
    """Reject a ``session_id`` that could traverse the filesystem.

    Mirrors the capture daemon's
    :func:`palace.daemons.capture.schema._validate_session_id` guard:
    ``/``, ``\\``, ``..``, null bytes, and empty/whitespace-only ids are
    rejected. Surfaces the CLI :class:`IndexError` so the ``transcript``
    error path renders a clean ``error: ...`` line.
    """
    if not session_id or not session_id.strip():
        raise IndexError("invalid session_id")
    if "/" in session_id or "\\" in session_id or ".." in session_id or "\x00" in session_id:
        raise IndexError("invalid session_id")
    return session_id


def _open_chunks_db_ro(db_path: Path) -> sqlite3.Connection:
    """Open the chunks DB read-only with sqlite-vec loaded."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _validated_metadata_filters(filters: Sequence[MetadataFilter]) -> tuple[MetadataFilter, ...]:
    try:
        return normalize_filters(filters)
    except MetadataError as exc:
        raise IndexError(str(exc)) from exc


def _metadata_constraint(
    filters: Sequence[MetadataFilter], column: str
) -> tuple[str, tuple[str, ...]]:
    if not filters:
        return "", ()
    sql, parameters = matching_chunks(filters)
    return f"AND {column} IN ({sql})", parameters


def _search_conn(
    conn: sqlite3.Connection,
    *,
    query: str,
    mode: SearchMode,
    limit: int,
    pool: int,
    rrf_k: int,
    embedder: Embedder | None,
    vector_query: str | None = None,
    filters: Sequence[MetadataFilter] = (),
    vector_backend: S3VectorsBackend | None = None,
    remote: dict[str, VectorMatch] | None = None,
) -> list[SearchHit]:
    """Run one retrieval pass through an already-open chunks connection.

    ``remote`` collects the vector leg's text-bearing matches so a caller can hydrate
    a match whose chunk has no local row.
    """
    bm25_rows = (
        _bm25_search(conn, query, pool, filters=filters) if mode in ("hybrid", "bm25") else []
    )
    vec_rows = (
        _vector_search(
            conn,
            query,
            pool,
            embedder,
            embed_text=vector_query,
            filters=filters,
            vector_backend=vector_backend,
        )
        if mode in ("hybrid", "vector")
        else []
    )
    if remote is not None:
        remote.update((match.chunk_id, match) for match in vec_rows if match.text is not None)
    return _fuse(conn, bm25_rows, vec_rows, limit, rrf_k)


_FTS5_TOKEN = re.compile(r"\w+")


def fts5_query(text: str) -> str:
    """Render free text as an FTS5 MATCH expression that cannot be syntax.

    FTS5's MATCH grammar treats punctuation as syntax: ``:`` is a column
    filter, ``"`` opens a phrase, ``(``/``)`` group, ``*`` is a prefix
    wildcard, ``-`` is NOT, ``^`` is initial-token, and ``;`` is simply
    invalid. Prose legitimately contains all of them, and the CLI is the
    surface a human types prose into — a semicolon in a question must not
    abort the search.

    Every run of word characters becomes one double-quoted FTS5 string and the
    strings are joined, which FTS5 ANDs together: the same term-conjunction
    semantics a bareword query already had, with punctuation made literal
    instead of syntactic. Quoting each token (rather than passing barewords)
    also neutralizes a token that happens to spell an operator — ``AND``,
    ``OR``, ``NOT``, ``NEAR`` — which FTS5 recognizes in upper case.

    The transform is idempotent: re-rendering an already-rendered expression
    yields the same string.

    :raises palace.index._errors.IndexError: when no searchable term survives
        (an all-punctuation or whitespace-only query). That is a real user
        error and stays loud rather than silently matching nothing.
    """
    tokens = _FTS5_TOKEN.findall(text)
    if not tokens:
        raise IndexError(f"query carries no searchable term: {text!r}")
    return " ".join(f'"{token}"' for token in tokens)


def _bm25_search(
    conn: sqlite3.Connection,
    query: str,
    pool: int,
    filters: Sequence[MetadataFilter] = (),
) -> list[tuple[str, str, float]]:
    """Return ``(chunk_id, snippet, bm25_score)`` for the top ``pool`` BM25 hits.

    ``query`` is free text. It is rendered through :func:`fts5_query` here, so
    no caller has to know FTS5's grammar and prose punctuation cannot abort a
    search. The vector leg deliberately receives the unrendered query: dense
    retrieval captures the phrase semantics this rendering drops.
    """
    match_expression = fts5_query(query)
    constraint, parameters = _metadata_constraint(filters, "chunks_fts.chunk_id")
    try:
        rows = conn.execute(
            f"""
            SELECT
                chunks_fts.chunk_id,
                snippet(chunks_fts, 1, '<<', '>>', '…', {_SNIPPET_TOKEN_BUDGET}) AS snippet,
                bm25(chunks_fts) AS score
            FROM chunks_fts
            WHERE chunks_fts MATCH ? {constraint}
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (match_expression, *parameters, pool),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # fts5_query renders any input into valid MATCH syntax, so reaching
        # here means a genuine engine or schema fault, not a punctuation
        # artifact. Surface a clean message rather than a SQL trace.
        raise IndexError(f"bm25 query failed: {exc}") from exc
    return [(cid, sn, float(score)) for cid, sn, score in rows]


def _vector_search(
    conn: sqlite3.Connection,
    query: str,
    pool: int,
    embedder: Embedder | None,
    *,
    embed_text: str | None = None,
    filters: Sequence[MetadataFilter] = (),
    vector_backend: S3VectorsBackend | None = None,
) -> list[VectorMatch]:
    """Return the top ``pool`` vector matches, nearest first.

    Without a backend this is the exhaustive sqlite-vec search of ``chunks_vec``. With
    the store's S3 Vectors backend the query is embedded the same way and sent to the
    published index; ``chunks_vec`` is not read.
    """
    assert_identity_readable(conn)
    if vector_backend is not None and filters:
        raise IndexError("metadata filters are not supported with the s3vectors vector backend")
    owned_embedder = embedder is None
    if embedder is None:
        embedder = OllamaEmbedder()
        try:
            embedder.probe()
        except IndexError:
            embedder.close()
            raise
    try:
        embeddings = embedder.embed([embed_text if embed_text is not None else query])
    finally:
        if owned_embedder:
            embedder.close()
    if not embeddings:
        return []
    if vector_backend is not None:
        return vector_backend.query(embeddings[0], pool)
    blob = sqlite_vec.serialize_float32(embeddings[0])
    constraint, parameters = _metadata_constraint(filters, "v.chunk_id")
    rows = conn.execute(
        f"""
        SELECT v.chunk_id, v.distance
        FROM chunks_vec v
        WHERE v.embedding MATCH ? AND k = ? {constraint}
        ORDER BY v.distance
        """,
        (blob, pool, *parameters),
    ).fetchall()
    return [VectorMatch(chunk_id=cid, distance=float(dist)) for cid, dist in rows]


def _fuse_variants(
    conn: sqlite3.Connection,
    *,
    query: str,
    mode: SearchMode,
    limit: int,
    pool: int,
    rrf_k: int,
    embedder: Embedder | None,
    expander: QueryExpander | None,
    hyde: bool,
    multi_query: int | None,
    filters: Sequence[MetadataFilter] = (),
    vector_backend: S3VectorsBackend | None = None,
    remote: dict[str, VectorMatch] | None = None,
) -> list[SearchHit]:
    """Run literal plus rewrite variants and union on each chunk's best RRF."""
    variants = [query]
    if multi_query is not None:
        if expander is None:
            raise IndexError("query expansion requested without an expander")
        variants.extend(expander.rewrites(query, multi_query))

    first_hit: dict[str, SearchHit] = {}
    best_score: dict[str, float] = {}
    for variant in variants:
        vector_query: str | None = None
        if hyde:
            if expander is None:
                raise IndexError("query expansion requested without an expander")
            vector_query = expander.hypothetical_passage(variant)
        hits = _search_conn(
            conn,
            query=variant,
            mode=mode,
            limit=limit,
            pool=pool,
            rrf_k=rrf_k,
            embedder=embedder,
            vector_query=vector_query,
            filters=filters,
            vector_backend=vector_backend,
            remote=remote,
        )
        for hit in hits:
            first_hit.setdefault(hit.chunk_id, hit)
            best_score[hit.chunk_id] = max(
                best_score.get(hit.chunk_id, float("-inf")), hit.rrf_score
            )

    ordered = sorted(best_score, key=lambda chunk_id: (-best_score[chunk_id], chunk_id))
    return [
        replace(first_hit[chunk_id], rrf_score=best_score[chunk_id]) for chunk_id in ordered[:limit]
    ]


def _hydrate_candidates(
    conn: sqlite3.Connection,
    chunk_ids: Sequence[str],
    remote: Mapping[str, VectorMatch] | None = None,
) -> list[RerankCandidate]:
    """Hydrate stored candidate fields in the caller's chunk-id order.

    A chunk with no local row is hydrated from its published text metadata when the
    vector leg returned it (``remote``); otherwise it is omitted.
    """
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT chunk_id, path, watch_root, heading, kind, body "
        f"FROM chunks WHERE chunk_id IN ({placeholders})",
        list(chunk_ids),
    ).fetchall()
    by_id = {
        chunk_id: RerankCandidate(
            chunk_id=chunk_id,
            path=path,
            watch_root=watch_root,
            heading=heading,
            kind=kind,
            body=body,
        )
        for chunk_id, path, watch_root, heading, kind, body in rows
    }
    for chunk_id in chunk_ids:
        match = (remote or {}).get(chunk_id)
        if chunk_id not in by_id and match is not None and match.text is not None:
            by_id[chunk_id] = RerankCandidate(
                chunk_id=chunk_id,
                path=match.path or "",
                watch_root="",
                heading=match.heading,
                kind="unknown",
                body=match.text,
            )
    return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def _validate_expansion(mode: SearchMode, hyde: bool, multi_query: int | None) -> None:
    if hyde and mode == "bm25":
        raise IndexError(
            "--hyde has no effect in --mode bm25 (HyDE replaces the vector leg's embedding input)"
        )
    if multi_query is not None and multi_query <= 0:
        raise IndexError("multi_query must be positive")


def _validate_retrieval(
    *,
    query: str,
    mode: SearchMode,
    limit: int,
    pool: int,
    hyde: bool,
    multi_query: int | None,
) -> None:
    if not query.strip():
        raise IndexError("search query is empty")
    if mode not in ("hybrid", "bm25", "vector"):
        raise IndexError(f"unknown search mode: {mode}")
    if limit <= 0 or pool <= 0:
        raise IndexError("limit and pool must be positive")
    _validate_expansion(mode, hyde, multi_query)


def _fuse(
    conn: sqlite3.Connection,
    bm25_rows: list[tuple[str, str, float]],
    vec_rows: list[VectorMatch],
    limit: int,
    rrf_k: int,
) -> list[SearchHit]:
    """Reciprocal-Rank-Fusion fuse the two pools; hydrate metadata for the top-N."""
    bm25_by_id: dict[str, tuple[int, str, float]] = {
        cid: (i + 1, snippet, score) for i, (cid, snippet, score) in enumerate(bm25_rows)
    }
    vec_by_id: dict[str, tuple[int, float]] = {
        match.chunk_id: (i + 1, match.distance) for i, match in enumerate(vec_rows)
    }
    remote_by_id = {match.chunk_id: match for match in vec_rows}

    candidates = set(bm25_by_id) | set(vec_by_id)
    if not candidates:
        return []

    scored: list[tuple[float, str]] = []
    for cid in candidates:
        bm25_term = 1.0 / (rrf_k + bm25_by_id[cid][0]) if cid in bm25_by_id else 0.0
        vec_term = 1.0 / (rrf_k + vec_by_id[cid][0]) if cid in vec_by_id else 0.0
        scored.append((bm25_term + vec_term, cid))
    # Sort by RRF score desc, then by chunk_id for deterministic ties.
    scored.sort(key=lambda row: (-row[0], row[1]))

    top = scored[:limit]
    top_ids = [cid for _, cid in top]

    # Hydrate metadata + body in one query. ``path`` is watch-root-relative;
    # ``watch_root`` is the absolute root, joined for the displayed path.
    placeholders = ",".join("?" * len(top_ids))
    meta_rows = conn.execute(
        f"SELECT chunk_id, path, watch_root, heading, body "
        f"FROM chunks WHERE chunk_id IN ({placeholders})",
        top_ids,
    ).fetchall()
    meta_by_id: dict[str, tuple[str, str, str | None, str]] = {
        cid: (path, watch_root, heading, body) for cid, path, watch_root, heading, body in meta_rows
    }

    hits: list[SearchHit] = []
    for rrf_score, cid in top:
        # A chunk with no local row shows its published metadata when the vector leg
        # carried it; otherwise the bare chunk_id stands in for the path.
        published = remote_by_id.get(cid)
        fallback = (
            (published.path or cid, "", published.heading, published.text or "")
            if published is not None and published.text is not None
            else (cid, "", None, "")
        )
        path, watch_root, heading, body = meta_by_id.get(cid, fallback)
        display_path = _resolve_path(watch_root, path) if watch_root else path
        if cid in bm25_by_id:
            bm25_rank, snippet, bm25_score = bm25_by_id[cid]
        else:
            bm25_rank, snippet, bm25_score = None, _preview(body), None
        vec_rank, vec_distance = (None, None)
        if cid in vec_by_id:
            vec_rank, vec_distance = vec_by_id[cid]
        hits.append(
            SearchHit(
                chunk_id=cid,
                path=display_path,
                heading=heading,
                snippet=snippet,
                rrf_score=rrf_score,
                bm25_rank=bm25_rank,
                bm25_score=bm25_score,
                vec_rank=vec_rank,
                vec_distance=vec_distance,
            )
        )
    return hits


def _resolve_path(watch_root: str, relative: str) -> str:
    """Resolve a stored watch-root-relative path against its absolute root.

    The single resolution point: ``chunks.path`` is relative (Phase 3.2)
    and ``chunks.watch_root`` is absolute, so the displayed, clickable
    path is their join.
    """
    return str(Path(watch_root) / relative)


def _preview(body: str) -> str:
    """Render a short body preview for vector-only hits (no BM25 snippet)."""
    flat = " ".join(body.split())
    if len(flat) <= _BODY_PREVIEW_CHARS:
        return flat
    return flat[:_BODY_PREVIEW_CHARS] + "…"


# --------------------------------------------------------------------- CLI


SEARCH_ESCALATION_COMMANDS: frozenset[str] = frozenset({"expand", "transcript"})
"""The two sub-subcommands :func:`main`'s argv-peek routes to a dedicated parser.

A bare ``palace search "<query>"`` mixes a ``nargs="+"`` positional with
no subparser, so the query terms never collide with these names. The
escalation tiers are parsed by their own parser
(:func:`build_escalation_parser`) which has no greedy query positional —
``palace search expand <chunk_id>`` and ``palace search transcript
<session_id>`` resolve cleanly without argparse trying to fold the
sub-subcommand token into the L1 query.
"""


def _positive_int(raw: str) -> int:
    """Argparse type for strictly-positive expansion and rerank pool sizes."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}")
    return value


def build_subparser(subparsers: Any) -> None:
    """Wire the L1 ``search`` subparser (bare query) into ``palace`` argparse.

    ``expand`` (L2) and ``transcript`` (L3) are NOT sub-subparsers here:
    argparse cannot mix a ``nargs="+"`` positional with a subparser
    action without the positional greedily swallowing the sub-subcommand
    token. :func:`main` peeks argv and routes those two to the dedicated
    :func:`build_escalation_parser` instead, leaving the bare-query path
    byte-identical.
    """
    search_p = subparsers.add_parser(
        "search",
        help="Hybrid BM25 + vector search over the chunks DB (+ expand / transcript).",
        description=(
            "Search the indexed chunks via BM25 (FTS5) and exhaustive vector "
            "(sqlite-vec + Ollama qwen3-embedding:8b, or the store's published S3 "
            "Vectors index when 'palace index vector-backend' selects it), fused by "
            "Reciprocal Rank Fusion. Optionally rerank with a local cross-encoder "
            "(auto-provisioned on first use; ./bin/palace-rerank-model supports "
            "explicit or offline setup). Read-only against "
            "<store>/index/chunks.sqlite. "
            "Escalate a hit with 'palace search expand <chunk_id>' (full "
            "section) or 'palace search transcript <session_id>' (raw "
            "captured dialogue)."
        ),
    )
    search_p.add_argument(
        "--metadata-filters",
        default=None,
        help="JSON array of exact metadata predicates (single store).",
    )
    search_p.add_argument(
        "query",
        nargs="+",
        help="Search query (multi-word queries are joined with spaces).",
    )
    search_p.add_argument(
        "--store",
        type=Path,
        action="append",
        default=None,
        help="Machine-state root; repeat for multi-store search (default: $PALACE_STORE).",
    )
    search_p.add_argument(
        "--allow-rerank-failure",
        action="store_true",
        help="Allow multi-store reranker failure to return rank fusion with failed status.",
    )
    search_p.add_argument(
        "--mode",
        choices=("hybrid", "bm25", "vector"),
        default="hybrid",
        help="Retrieval mode; hybrid (default) fuses BM25 + vector via RRF.",
    )
    search_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            f"Number of results to return (default: {RERANK_OUT} with reranking, including "
            f"the default path; {DEFAULT_LIMIT} with --no-rerank)."
        ),
    )
    search_p.add_argument(
        "--pool",
        type=int,
        default=DEFAULT_POOL,
        help=(
            f"Candidate pool size per retriever before fusion "
            f"(default: {DEFAULT_POOL}). Wider pools surface more "
            "consensus hits at the cost of an extra few ms per query."
        ),
    )
    search_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit one JSON object per result to stdout (jsonl).",
    )
    search_p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include per-retriever rank + score in the human-readable output.",
    )
    search_p.add_argument(
        "--rerank",
        action=argparse.BooleanOptionalAction,
        default=RERANK_ENABLED_DEFAULT,
        help=(
            "Refine the fused pool with the local cross-encoder "
            "(default: on; --no-rerank opts out)."
        ),
    )
    search_p.add_argument(
        "--rerank-in",
        type=_positive_int,
        default=RERANK_IN,
        help=f"Fused candidate depth sent to the cross-encoder (default: {RERANK_IN}).",
    )
    search_p.add_argument(
        "--hyde",
        action="store_true",
        help="Use a local hypothetical passage for each vector-retrieval leg.",
    )
    search_p.add_argument(
        "--multi-query",
        nargs="?",
        type=_positive_int,
        const=MULTI_QUERY_N,
        default=None,
        metavar="N",
        help=f"Add N local query rewrites before fusion (bare flag: {MULTI_QUERY_N}).",
    )


def build_escalation_parser() -> argparse.ArgumentParser:
    """Build the standalone parser for ``search expand`` / ``search transcript``.

    Invoked by :func:`main` only when argv peeks as ``search expand ...``
    or ``search transcript ...``. ``prog`` is set to ``palace search`` so
    ``--help`` and error usage lines read naturally. Sets
    ``search_command`` so :func:`dispatch_search` routes.
    """
    parser = argparse.ArgumentParser(
        prog="palace search",
        description=(
            "Recall-escalation tiers L2/L3 over a 'palace search' hit: "
            "'expand <chunk_id>' widens a hit to its full Markdown section; "
            "'transcript <session_id>' returns a captured session's raw JSONL."
        ),
    )
    escalation_sub = parser.add_subparsers(dest="search_command", required=True)

    expand_p = escalation_sub.add_parser(
        "expand",
        help="L2 — widen a search hit to its full Markdown section.",
        description=(
            "Given a chunk_id from a 'palace search' hit, return the full "
            "section the snippet was clipped from. Read-only against "
            "<store>/index/chunks.sqlite."
        ),
    )
    expand_p.add_argument("chunk_id", help="The chunk_id to expand (from a search hit).")
    expand_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    expand_p.add_argument(
        "--neighbors",
        type=int,
        default=0,
        help="Also return sibling sections within ±N section_index (default: 0).",
    )
    expand_p.add_argument(
        "--around",
        action="store_true",
        help="Shorthand for --neighbors 1 (the immediately adjacent sections).",
    )
    expand_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit one JSON object to stdout.",
    )

    transcript_p = escalation_sub.add_parser(
        "transcript",
        help="L3 — return a captured session's raw JSONL transcript.",
        description=(
            "Given a session_id the capture daemon wrote, print that "
            "session's transcript, resolving its dated directory under "
            "<store>/sessions/ automatically. Read-only."
        ),
    )
    transcript_p.add_argument("session_id", help="The captured session id to print.")
    transcript_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    transcript_p.add_argument(
        "--raw",
        action="store_true",
        dest="raw_output",
        help="Emit the verbatim captured JSONL lines (one per turn).",
    )
    transcript_p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit the verbatim captured JSONL lines (alias of --raw).",
    )
    return parser


def dispatch_search(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """``palace search ...`` entry point — route among L1 / expand / transcript."""
    del parser  # mirrors other dispatchers' signature; unused here
    search_command = getattr(args, "search_command", None)
    if search_command == "expand":
        return cli_expand(args)
    if search_command == "transcript":
        return cli_transcript(args)
    return cli_search(args)


def cli_search(args: Any) -> int:
    """Run the selected L1 route; report any S3 Vectors usage on stderr afterwards.

    Stdout keeps its hit-line contract. Whenever the route made S3 Vectors requests,
    success or failure, one stderr line reports them: text, or under ``--json`` one
    JSON object ``{"vector_usage": {...}}``.
    """
    usage = VectorUsage()
    try:
        with vector_usage_scope(usage):
            return _cli_search(args)
    finally:
        _emit_vector_usage(usage.summary(), json_output=bool(getattr(args, "json_output", False)))


def _emit_vector_usage(summary: VectorUsageSummary, *, json_output: bool) -> None:
    if not summary.requests:
        return
    if json_output:
        line = json.dumps({"vector_usage": summary.as_dict()}, sort_keys=True)
    else:
        line = "palace search: vector backend s3vectors" + summary.log_fields()
    print(line, file=sys.stderr, flush=True)


def _cli_search(args: Any) -> int:
    """Run the selected L1 retrieval/expansion/reranking route."""
    try:
        raw_filters = getattr(args, "metadata_filters", None)
        filters = parse_filters(json.loads(raw_filters)) if raw_filters is not None else ()
        if filters and args.store and len(args.store) > 1:
            raise MetadataError("metadata filters require a single store")
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    if isinstance(args.store, list) and len(args.store) > 1:
        from palace.multistore import emit_results, search_stores

        query = " ".join(args.query).strip()
        limit = (
            args.limit if args.limit is not None else (RERANK_OUT if args.rerank else DEFAULT_LIMIT)
        )
        try:
            multiple = search_stores(
                query=query,
                stores=args.store,
                mode=args.mode,
                limit=limit,
                pool=args.pool,
                rerank_in=args.rerank_in,
                rerank_enabled=bool(args.rerank),
                strict=not args.allow_rerank_failure,
                hyde=args.hyde,
                multi_query=args.multi_query,
            )
        except IndexError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return 1
        emit_results(multiple, query=query, json_output=args.json_output, verbose=args.verbose)
        return 0
    store = _resolve_store(args)
    query = " ".join(args.query).strip()
    if not query:
        print("error: search query is empty", file=sys.stderr, flush=True)
        return 1
    rerank_on = bool(args.rerank)
    expansion_on = bool(args.hyde or args.multi_query is not None)
    limit = args.limit if args.limit is not None else (RERANK_OUT if rerank_on else DEFAULT_LIMIT)
    status: RerankStatus = "disabled"
    reranker_identity: RerankIdentity | None = None
    try:
        if rerank_on:
            result = search_reranked(
                query=query,
                store=store,
                filters=filters,
                rerank_in=args.rerank_in,
                limit=limit,
                mode=args.mode,
                pool=args.pool,
                hyde=args.hyde,
                multi_query=args.multi_query,
                strict=False if args.allow_rerank_failure else None,
            )
            hits = result.hits
            status = result.status
            reranker_identity = result.reranker_identity
        elif expansion_on:
            hits = search_expanded(
                query=query,
                store=store,
                filters=filters,
                mode=args.mode,
                limit=limit,
                pool=args.pool,
                hyde=args.hyde,
                multi_query=args.multi_query,
            )
        else:
            hits = search(
                query=query,
                store=store,
                filters=filters,
                mode=args.mode,
                limit=limit,
                pool=args.pool,
            )
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.json_output:
        _emit_jsonl(hits, status=status, reranker_identity=reranker_identity)
    else:
        _emit_human(hits, query=query, mode=args.mode, verbose=args.verbose)
    return 0


def cli_expand(args: Any) -> int:
    """Run :func:`expand` (L2) and render the section to stdout."""
    store = _resolve_store(args)
    neighbors = 1 if getattr(args, "around", False) else args.neighbors
    try:
        result = expand(chunk_id=args.chunk_id, store=store, neighbors=neighbors)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.json_output:
        _emit_expand_json(result)
    else:
        _emit_expand_human(result)
    return 0


def cli_transcript(args: Any) -> int:
    """Run :func:`transcript` (L3) and render the session to stdout."""
    store = _resolve_store(args)
    try:
        result = transcript(session_id=args.session_id, store=store)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.json_output or args.raw_output:
        _emit_transcript_raw(result)
    else:
        _emit_transcript_human(result)
    return 0


def _resolve_store(args: Any) -> Path:
    """Mirror the other subcommands' ``--store`` precedence."""
    import os

    if getattr(args, "store", None) is not None:
        value = args.store[0] if isinstance(args.store, list) else args.store
        return Path(value).expanduser()
    env = os.environ.get(_PALACE_STORE_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE


def _emit_jsonl(
    hits: list[SearchHit], *, status: RerankStatus, reranker_identity: RerankIdentity | None = None
) -> None:
    for hit in hits:
        payload = {
            "chunk_id": hit.chunk_id,
            "path": hit.path,
            "heading": hit.heading,
            "snippet": hit.snippet,
            "rrf_score": hit.rrf_score,
            "bm25_rank": hit.bm25_rank,
            "bm25_score": hit.bm25_score,
            "vec_rank": hit.vec_rank,
            "vec_distance": hit.vec_distance,
            "rerank_rank": hit.rerank_rank,
            "rerank_score": hit.rerank_score,
            "rerank_status": status,
            "reranker_identity": asdict(reranker_identity)
            if reranker_identity is not None
            else None,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def _emit_human(hits: list[SearchHit], *, query: str, mode: SearchMode, verbose: bool) -> None:
    if not hits:
        print(f"(no results for {query!r} in mode={mode})", flush=True)
        return
    for i, hit in enumerate(hits, start=1):
        head = f" [{hit.heading}]" if hit.heading else ""
        print(f"{i:>2}. {hit.path}{head}", flush=True)
        print(f"    {hit.snippet}", flush=True)
        if verbose:
            parts: list[str] = [f"rrf={hit.rrf_score:.4f}"]
            if hit.bm25_rank is not None:
                parts.append(f"bm25=#{hit.bm25_rank} ({hit.bm25_score:.2f})")
            if hit.vec_rank is not None:
                parts.append(f"vec=#{hit.vec_rank} ({hit.vec_distance:.4f})")
            if hit.rerank_rank is not None and hit.rerank_score is not None:
                parts.append(f"ce=#{hit.rerank_rank} ({hit.rerank_score:.3f})")
            print(f"    {' '.join(parts)}", flush=True)


def _emit_expand_human(result: ExpandResult) -> None:
    head = f" [{result.heading}]" if result.heading else ""
    print(f"{result.path}{head}", flush=True)
    print(f"({result.kind} · {result.chunk_id})", flush=True)
    print("", flush=True)
    print(result.body, flush=True)
    for section in result.neighbors:
        print("", flush=True)
        s_head = f" [{section.heading}]" if section.heading else ""
        print(f"--- §{section.section_index}.{section.window_index}{s_head} ---", flush=True)
        print(section.body, flush=True)


def _emit_expand_json(result: ExpandResult) -> None:
    payload = {
        "chunk_id": result.chunk_id,
        "path": result.path,
        "heading": result.heading,
        "kind": result.kind,
        "body": result.body,
        "neighbors": [
            {
                "section_index": section.section_index,
                "window_index": section.window_index,
                "heading": section.heading,
                "body": section.body,
            }
            for section in result.neighbors
        ],
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def _emit_transcript_human(result: TranscriptResult) -> None:
    for i, record in enumerate(result.records, start=1):
        event_type = record.get("event_type") or "?"
        harness = record.get("harness") or "?"
        ingest_time = record.get("ingest_time") or "?"
        print(f"# turn {i} — {event_type} · {harness} · {ingest_time}", flush=True)

        message = record.get("assistant_message")
        if message:
            print(message, flush=True)

        tool_calls = record.get("tool_calls")
        if tool_calls:
            summaries: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                name = call.get("name") or call.get("tool") or "?"
                arguments = call.get("arguments")
                arg_keys = sorted(arguments) if isinstance(arguments, dict) else []
                summaries.append(f"{name}({', '.join(arg_keys)})" if arg_keys else f"{name}()")
            if summaries:
                print(f"  tools: {'; '.join(summaries)}", flush=True)

        files_touched = record.get("files_touched")
        if files_touched:
            names: list[str] = []
            for entry in files_touched:
                if isinstance(entry, dict):
                    names.append(str(entry.get("path") or entry.get("file") or entry))
                else:
                    names.append(str(entry))
            if names:
                print(f"  files: {', '.join(names)}", flush=True)

        print("", flush=True)


def _emit_transcript_raw(result: TranscriptResult) -> None:
    for line in result.raw_lines:
        print(line, flush=True)
