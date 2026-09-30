"""Identity-bound retrieval and one rerank across explicit stores."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from palace.index._errors import IndexError
from palace.index.config import EmbeddingIdentity, chunks_db_path
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    assert_remote_corpus_boundary,
    identity_for,
    load_embedder_config,
    resolve_embedder,
)
from palace.index.s3vectors import (
    S3VectorsBackend,
    VectorMatch,
    VectorUsageSummary,
    resolve_vector_backend,
)
from palace.index.schema import assert_identity, assert_identity_readable
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
from palace.retrieval_config import RERANK_IN, RERANK_OUT
from palace.search import (
    DEFAULT_POOL,
    RRF_K,
    SearchHit,
    SearchMode,
    _fuse_variants,
    _hydrate_candidates,
    _open_chunks_db_ro,
    _served_usage,
    _tracked_vector_usage,
    _validate_retrieval,
)


@dataclass(frozen=True)
class StoreHit:
    """A local chunk identifier is meaningful only with its canonical store."""

    store: Path
    hit: SearchHit

    @property
    def origin_id(self) -> tuple[str, str]:
        return str(self.store), self.hit.chunk_id


@dataclass(frozen=True)
class MultiStoreSearch:
    hits: list[StoreHit]
    status: RerankStatus
    detail: str | None = None
    reranker_identity: RerankIdentity | None = None
    vector_usage: VectorUsageSummary | None = None


class _QueryVectors:
    """One query-vector cache per recorded identity, for this invocation only."""

    max_concurrency = 1

    def __init__(self, delegate: Embedder, dim: int) -> None:
        self.delegate = delegate
        self.dim = dim
        self.cache: dict[str, list[float]] = {}

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        missing = list(dict.fromkeys(text for text in texts if text not in self.cache))
        if missing:
            vectors = self.delegate.embed(missing)
            if len(vectors) != len(missing):
                raise IndexError("query embedder returned the wrong vector count")
            for vector in vectors:
                if len(vector) != self.dim or not all(math.isfinite(v) for v in vector):
                    raise IndexError(
                        "query embedder returned an invalid dimension or non-finite value"
                    )
            self.cache.update(zip(missing, vectors, strict=True))
        return [self.cache[text] for text in texts]

    def probe(self) -> None:
        self.delegate.probe()

    def close(self) -> None:
        # The invocation owns constructed delegates; injected ones stay caller-owned.
        pass


class _QueryExpansion:
    """Reuse the same expansion across stores, including nondeterministic models."""

    def __init__(self, delegate: QueryExpander) -> None:
        self.delegate = delegate
        self.passages: dict[str, str] = {}
        self.variants: dict[tuple[str, int], list[str]] = {}

    def hypothetical_passage(self, query: str) -> str:
        if query not in self.passages:
            self.passages[query] = self.delegate.hypothetical_passage(query)
        return self.passages[query]

    def rewrites(self, query: str, count: int) -> list[str]:
        key = query, count
        if key not in self.variants:
            self.variants[key] = list(self.delegate.rewrites(query, count))
        return list(self.variants[key])

    def probe(self) -> None:
        self.delegate.probe()

    def close(self) -> None:
        pass


def search_stores(
    *,
    query: str,
    stores: Sequence[Path],
    mode: SearchMode = "hybrid",
    limit: int = RERANK_OUT,
    pool: int = DEFAULT_POOL,
    rrf_k: int = RRF_K,
    rerank_in: int = RERANK_IN,
    rerank_enabled: bool = True,
    strict: bool = True,
    hyde: bool = False,
    multi_query: int | None = None,
    embedders: Mapping[EmbeddingIdentity, Embedder] | None = None,
    expander: QueryExpander | None = None,
    reranker: Reranker | None = None,
) -> MultiStoreSearch:
    """Retrieve each store in its own vector space, then rerank the union once.

    Duplicate, absent and incompatible stores refuse the whole request. A vector
    request binds each recorded identity to that store's explicit provider config.
    Remote calls retain their ordinary boundary and audit obligations. Databases
    are read-only; a remote provider may append its required egress audit. Injected
    providers are caller-owned and are never substituted or silently selected.
    """
    _validate_retrieval(
        query=query, mode=mode, limit=limit, pool=pool, hyde=hyde, multi_query=multi_query
    )
    if not stores or rerank_in <= 0 or rrf_k <= 0:
        raise IndexError("stores must be non-empty; rerank_in and rrf_k must be positive")
    roots = [Path(store).expanduser().resolve() for store in stores]
    if len(set(roots)) != len(roots):
        raise IndexError("duplicate store (including aliases of the same directory)")
    roots.sort()
    try:
        with ExitStack() as stack:
            usage = stack.enter_context(_tracked_vector_usage())
            connections: dict[Path, sqlite3.Connection] = {}
            identities: dict[Path, EmbeddingIdentity] = {}
            watch_roots_by_store: dict[Path, list[Path]] = {}
            reranker_config: RerankConfig | None = None
            # Validate the entire request before constructing any provider or sending
            # any input. Keep read snapshots open through retrieval and hydration.
            for root in roots:
                db = chunks_db_path(root)
                if not db.is_file():
                    raise IndexError(f"store has no chunks database: {root}")
                conn = _open_chunks_db_ro(db)
                stack.callback(conn.close)
                conn.execute("BEGIN")
                connections[root] = conn
                watch_roots = [
                    Path(row[0]) for row in conn.execute("SELECT DISTINCT watch_root FROM chunks")
                ]
                watch_roots_by_store[root] = watch_roots
                if rerank_enabled and reranker is None:
                    selected_reranker = load_rerank_config(root)
                    preflight_reranker(selected_reranker, store=root, watch_roots=watch_roots)
                    if reranker_config is None:
                        reranker_config = selected_reranker
                    elif reranker_config.identity != selected_reranker.identity:
                        raise IndexError(
                            "multi-store reranker identities must match for one union rerank"
                        )
                if mode != "bm25":
                    config = load_embedder_config(root)
                    identity = identity_for(config)
                    assert_identity_readable(conn)
                    assert_identity(conn, identity)
                    assert_remote_corpus_boundary(
                        config=config, store=root, watch_roots=watch_roots
                    )
                    identities[root] = identity
                    if config.endpoint is not None and embedders is None:
                        config.endpoint.runtime()
            if embedders is not None and any(key not in embedders for key in identities.values()):
                raise IndexError("no injected query embedder for a recorded embedding identity")
            # Each store's selected vector leg: sqlite-vec, or its published S3 Vectors
            # index after its receipt and identity checks. No request is sent here.
            backends: dict[Path, S3VectorsBackend | None] = {
                root: resolve_vector_backend(
                    store=root, identity=identity, watch_roots=watch_roots_by_store[root]
                )
                for root, identity in identities.items()
            }

            vectors: dict[EmbeddingIdentity, _QueryVectors] = {}
            for root, identity in identities.items():
                if identity in vectors:
                    continue
                if embedders is not None:
                    delegate = embedders[identity]
                else:
                    delegate, resolved = resolve_embedder(root)
                    stack.callback(delegate.close)
                    if resolved != identity:
                        raise IndexError("provider identity changed during query preparation")
                vectors[identity] = _QueryVectors(delegate, identity.dim)
            if hyde or multi_query is not None:
                if expander is None:
                    expander = OllamaQueryExpander()
                    stack.callback(expander.close)
                    expander.probe()
                expander = _QueryExpansion(expander)

            union: dict[str, StoreHit] = {}
            candidates: dict[str, RerankCandidate] = {}
            for root, conn in connections.items():
                remote: dict[str, VectorMatch] = {}
                hits = _fuse_variants(
                    conn,
                    query=query,
                    mode=mode,
                    limit=rerank_in,
                    pool=pool,
                    rrf_k=rrf_k,
                    embedder=vectors[identities[root]] if root in identities else None,
                    expander=expander,
                    hyde=hyde,
                    multi_query=multi_query,
                    vector_backend=backends.get(root),
                    remote=remote,
                )
                hydrated = _hydrate_candidates(conn, [hit.chunk_id for hit in hits], remote)
                if len(hydrated) != len(hits):
                    raise IndexError(f"candidate hydration lost a chunk in store: {root}")
                for rank, (hit, candidate) in enumerate(zip(hits, hydrated, strict=True), start=1):
                    item = StoreHit(root, replace(hit, rrf_score=1.0 / (rrf_k + rank)))
                    key = json.dumps(item.origin_id, ensure_ascii=False, separators=(",", ":"))
                    union[key] = item
                    candidates[key] = replace(candidate, chunk_id=key)
            fused = sorted(union, key=lambda key: (-union[key].hit.rrf_score, key))
            if not rerank_enabled:
                return MultiStoreSearch(
                    [union[key] for key in fused[:limit]],
                    "disabled",
                    vector_usage=_served_usage(usage),
                )
            reranker_identity = reranker_config.identity if reranker_config is not None else None
            if not fused:
                return MultiStoreSearch(
                    [],
                    "applied",
                    reranker_identity=reranker_identity,
                    vector_usage=_served_usage(usage),
                )
            try:
                if reranker is None:
                    assert reranker_config is not None
                    reranker = construct_reranker(
                        reranker_config, store=roots[0], local_loader=load_reranker
                    )
                    stack.callback(reranker.close)
                ranked = rerank(
                    query=query,
                    candidates=[candidates[key] for key in fused],
                    top_k=limit,
                    reranker=reranker,
                )
            except RerankUnavailableError as exc:
                if strict:
                    raise
                _warn_rerank_failed(
                    detail=str(exc),
                    private=reranker_identity is not None
                    and reranker_identity.provider.startswith("endpoint:"),
                )
                return MultiStoreSearch(
                    [union[key] for key in fused[:limit]],
                    "failed",
                    str(exc),
                    reranker_identity,
                    vector_usage=_served_usage(usage),
                )
            return MultiStoreSearch(
                [
                    replace(
                        union[row.candidate.chunk_id],
                        hit=replace(
                            union[row.candidate.chunk_id].hit,
                            rerank_rank=row.rank,
                            rerank_score=row.score,
                        ),
                    )
                    for row in ranked
                ],
                "applied",
                reranker_identity=reranker_identity,
                vector_usage=_served_usage(usage),
            )
    except sqlite3.Error as exc:
        raise IndexError(f"multi-store database read failed: {exc}") from exc


def emit_results(result: MultiStoreSearch, *, query: str, json_output: bool, verbose: bool) -> None:
    """Render exact origin alongside each local chunk identifier."""
    for rank, item in enumerate(result.hits, start=1):
        if json_output:
            print(
                json.dumps(
                    {
                        **asdict(item.hit),
                        "store": str(item.store),
                        "origin_id": item.origin_id,
                        "rerank_status": result.status,
                        "reranker_identity": (
                            asdict(result.reranker_identity) if result.reranker_identity else None
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
        else:
            print(f"{rank:>2}. [{item.store}] {item.hit.path}", flush=True)
            print(f"    {item.hit.snippet}", flush=True)
            if verbose:
                print(
                    f"    chunk={item.hit.chunk_id} rrf={item.hit.rrf_score:.4f} "
                    f"rerank={result.status} score={item.hit.rerank_score}",
                    flush=True,
                )
    if not result.hits and not json_output:
        print(f"(no results for {query!r})", flush=True)
