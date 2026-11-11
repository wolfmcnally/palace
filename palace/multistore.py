"""Identity-bound retrieval and one rerank across explicit stores."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from palace.index._errors import IndexError
from palace.index.config import EmbeddingIdentity, chunks_db_path
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    EmbedderConfig,
    assert_remote_corpus_boundary,
    identity_for,
    load_embedder_config,
    resolve_embedder,
)
from palace.index.s3vectors import (
    BACKEND_S3VECTORS,
    PublishedIndex,
    S3VectorsBackend,
    VectorBackendConfig,
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
    SQLITE_VEC_MAX_K,
    SearchHit,
    SearchMode,
    _fuse_variants,
    _hydrate_candidates,
    _open_chunks_db_ro,
    _served_usage,
    _tracked_vector_usage,
    _validate_retrieval,
    fts5_query,
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


class _PreparedVectors:
    """Serve prepared query vectors; execution never embeds."""

    max_concurrency = 1

    def __init__(self, vectors: Mapping[str, tuple[float, ...]]) -> None:
        self.vectors = vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if any(text not in self.vectors for text in texts):
            raise IndexError("prepared search has no vector for this input; execution never embeds")
        return [list(self.vectors[text]) for text in texts]

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


class _PreparedExpansion:
    """Serve prepared rewrites and passages; execution never expands."""

    def __init__(self, prepared: PreparedStoreSearch) -> None:
        self.prepared = prepared
        self.passages = dict(prepared.passages)

    def hypothetical_passage(self, query: str) -> str:
        if query not in self.passages:
            raise IndexError(
                "prepared search has no expansion for this input; execution never expands"
            )
        return self.passages[query]

    def rewrites(self, query: str, count: int) -> list[str]:
        prepared = self.prepared
        if prepared.rewrites is None or query != prepared.query or count != prepared.multi_query:
            raise IndexError(
                "prepared search has no expansion for this input; execution never expands"
            )
        return list(prepared.rewrites)

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _StoreBinding:
    """Stable configuration one store's prepared search depends on, never its corpus."""

    store: Path
    embedder: EmbedderConfig | None
    identity: EmbeddingIdentity | None
    vector_backend: VectorBackendConfig | None
    published_index: PublishedIndex | None
    reranker: RerankConfig | None


@dataclass
class _Admission:
    connections: dict[Path, sqlite3.Connection]
    identities: dict[Path, EmbeddingIdentity]
    reranker_config: RerankConfig | None
    backends: dict[Path, S3VectorsBackend | None]
    bindings: tuple[_StoreBinding, ...]


def _admit_stores(
    roots: Sequence[Path],
    *,
    mode: SearchMode,
    pool: int,
    rerank_enabled: bool,
    injected_reranker: bool,
    for_query_embedding: bool,
    embedders: Mapping[EmbeddingIdentity, Embedder] | None,
    stack: ExitStack,
) -> _Admission:
    """Validate every store before any provider is constructed or sent any input.

    Read snapshots stay open on ``stack``. Vector backends are resolved without a
    request. ``for_query_embedding`` also checks a to-be-constructed endpoint
    embedder's runtime material; execution constructs no embedder and skips it.
    """
    connections: dict[Path, sqlite3.Connection] = {}
    identities: dict[Path, EmbeddingIdentity] = {}
    configs: dict[Path, EmbedderConfig] = {}
    watch_roots_by_store: dict[Path, list[Path]] = {}
    rerank_configs: dict[Path, RerankConfig] = {}
    reranker_config: RerankConfig | None = None
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
        if rerank_enabled and not injected_reranker:
            selected_reranker = load_rerank_config(root)
            preflight_reranker(selected_reranker, store=root, watch_roots=watch_roots)
            rerank_configs[root] = selected_reranker
            if reranker_config is None:
                reranker_config = selected_reranker
            elif reranker_config.identity != selected_reranker.identity:
                raise IndexError("multi-store reranker identities must match for one union rerank")
        if mode != "bm25":
            config = load_embedder_config(root)
            identity = identity_for(config)
            assert_identity_readable(conn)
            assert_identity(conn, identity)
            assert_remote_corpus_boundary(config=config, store=root, watch_roots=watch_roots)
            identities[root] = identity
            configs[root] = config
            if config.endpoint is not None and for_query_embedding and embedders is None:
                config.endpoint.runtime()
    if embedders is not None and any(key not in embedders for key in identities.values()):
        raise IndexError("no injected query embedder for a recorded embedding identity")
    # Each store's selected vector leg: sqlite-vec, or its published S3 Vectors index
    # after its receipt and identity checks. No request is sent here.
    backends: dict[Path, S3VectorsBackend | None] = {
        root: resolve_vector_backend(
            store=root, identity=identity, watch_roots=watch_roots_by_store[root]
        )
        for root, identity in identities.items()
    }
    # Deterministic vector-leg bounds, after every security check and before any
    # provider is constructed, with the texts retrieval itself would raise.
    for root, identity in identities.items():
        backend = backends[root]
        if backend is not None:
            backend.check_query(dimension=identity.dim, top_k=pool)
        elif pool > SQLITE_VEC_MAX_K:
            raise IndexError(
                "multi-store database read failed: k value in knn query too large, "
                f"provided {pool} and the limit is {SQLITE_VEC_MAX_K}"
            )
    bindings = tuple(
        _StoreBinding(
            store=root,
            embedder=configs.get(root),
            identity=identities.get(root),
            vector_backend=_selected_backend(backends[root]) if root in backends else None,
            published_index=(
                backend.published_index if (backend := backends.get(root)) is not None else None
            ),
            reranker=rerank_configs.get(root),
        )
        for root in roots
    )
    return _Admission(connections, identities, reranker_config, backends, bindings)


def _selected_backend(backend: S3VectorsBackend | None) -> VectorBackendConfig:
    """The selector a resolved backend was built from, without a second read."""
    if backend is None:
        return VectorBackendConfig()
    return VectorBackendConfig(BACKEND_S3VECTORS, backend.profile)


def _changed_axis(prepared: _StoreBinding, current: _StoreBinding) -> str | None:
    for axis, before, after in (
        ("identity", prepared.identity, current.identity),
        ("embedder", prepared.embedder, current.embedder),
        ("vector backend", prepared.vector_backend, current.vector_backend),
        ("published index", prepared.published_index, current.published_index),
        ("reranker", prepared.reranker, current.reranker),
    ):
        if before != after:
            return axis
    return None


# Drawn at import: a prepared value verifies in this process and in forked children
# that inherit the key, never in a spawned or separate process.
_SEAL_KEY = secrets.token_bytes(32)


def _seal(values: tuple[object, ...]) -> str:
    message = repr(values).encode("utf-8", "backslashreplace")
    return hmac.new(_SEAL_KEY, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedStoreSearch:
    """A validated, expanded and embedded multi-store query, ready to execute.

    The value is immutable. It holds no connection, snapshot, provider or caller
    object, and binds only stable query, store, provider and backend identity, never
    corpus content: execution searches each store's current rows. Construct it only
    with :func:`prepare_store_search`. It is sealed with a key drawn when this module
    is imported, so :func:`execute_prepared` refuses an altered or hand-built value,
    or one from a process that did not inherit that key (a spawned or separate
    process; a forked child inherits it).
    """

    query: str
    mode: SearchMode
    limit: int
    pool: int
    rrf_k: int
    rerank_in: int
    rerank_enabled: bool
    strict: bool
    hyde: bool
    multi_query: int | None
    injected_reranker: bool
    stores: tuple[Path, ...]
    bindings: tuple[_StoreBinding, ...]
    rewrites: tuple[str, ...] | None
    passages: tuple[tuple[str, str], ...]
    vectors: tuple[tuple[EmbeddingIdentity, tuple[tuple[str, tuple[float, ...]], ...]], ...]
    seal: str = field(repr=False)


def _unsealed(prepared: PreparedStoreSearch) -> tuple[object, ...]:
    return tuple(getattr(prepared, item.name) for item in fields(prepared) if item.name != "seal")


def prepare_store_search(
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
    injected_reranker: bool = False,
) -> PreparedStoreSearch:
    """Admit the whole request, then expand and embed the query, holding nothing after.

    Every refusal :func:`search_stores` makes before provider input happens here first.
    Expansion and embedding then make exactly the calls a single search would: one
    rewrite request, one passage per distinct variant, and one embedding per distinct
    input per recorded identity. Constructed providers close before return; injected
    ones stay caller-owned. ``injected_reranker`` declares that execution will receive
    a caller-owned reranker, so the configured one is not preflighted.
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
        with ExitStack() as admission_stack:
            admission = _admit_stores(
                roots,
                mode=mode,
                pool=pool,
                rerank_enabled=rerank_enabled,
                injected_reranker=injected_reranker,
                for_query_embedding=True,
                embedders=embedders,
                stack=admission_stack,
            )
    except sqlite3.Error as exc:
        raise IndexError(f"multi-store database read failed: {exc}") from exc
    identities = admission.identities
    with ExitStack() as stack:
        vectors: dict[EmbeddingIdentity, _QueryVectors] = {}
        for root, identity in identities.items():
            if identity in vectors:
                continue
            if embedders is not None:
                delegate = embedders[identity]
            else:
                delegate, resolved = resolve_embedder(root, purpose="query")
                stack.callback(delegate.close)
                if resolved != identity:
                    raise IndexError("provider identity changed during query preparation")
            vectors[identity] = _QueryVectors(delegate, identity.dim)
        expansion: _QueryExpansion | None = None
        if hyde or multi_query is not None:
            if expander is None:
                expander = OllamaQueryExpander()
                stack.callback(expander.close)
                expander.probe()
            expansion = _QueryExpansion(expander)
        # The variants and embedding inputs _fuse_variants derives, with a single
        # search's per-provider calls, inputs and order; only their interleaving with
        # vector queries moves. Rewrites once; for the first identity, per variant, its
        # passage, the BM25 leg's term check and its embedding; then each later
        # identity's embeddings.
        variants = [query]
        rewrites: tuple[str, ...] | None = None
        if multi_query is not None:
            assert expansion is not None
            rewrites = tuple(expansion.rewrites(query, multi_query))
            variants.extend(rewrites)
        passages: dict[str, str] = {}
        caches = list(vectors.values())
        for variant in variants:
            if hyde:
                assert expansion is not None
                passages[variant] = expansion.hypothetical_passage(variant)
            if mode == "hybrid":
                fts5_query(variant)
            if caches:
                caches[0].embed([passages[variant] if hyde else variant])
        for cache in caches[1:]:
            for variant in variants:
                cache.embed([passages[variant] if hyde else variant])
        frozen_vectors = tuple(
            (
                identity,
                tuple(
                    (text, tuple(float(value) for value in vector))
                    for text, vector in cache.cache.items()
                ),
            )
            for identity, cache in vectors.items()
        )
    values = (
        query,
        mode,
        limit,
        pool,
        rrf_k,
        rerank_in,
        rerank_enabled,
        strict,
        hyde,
        multi_query,
        injected_reranker,
        tuple(roots),
        admission.bindings,
        rewrites,
        tuple(passages.items()),
        frozen_vectors,
    )
    return PreparedStoreSearch(*values, seal=_seal(values))


def execute_prepared(
    prepared: PreparedStoreSearch, *, reranker: Reranker | None = None
) -> MultiStoreSearch:
    """Freshly admit each prepared store and search its current rows with prepared vectors.

    Rows added or removed since preparation are searched as they are now. A change to a
    store's embedder selector, recorded identity, vector-backend selector, published
    index or configured reranker refuses before any vector query or rerank, as does
    every refusal admission makes. Execution never embeds or expands. An S3 Vectors
    store still receives its query request; reranking and ``strict`` behave as in
    :func:`search_stores`.
    """
    if not isinstance(prepared, PreparedStoreSearch) or not hmac.compare_digest(
        prepared.seal, _seal(_unsealed(prepared))
    ):
        raise IndexError("prepared search was altered or not prepared in this process")
    if (reranker is not None) != prepared.injected_reranker:
        raise IndexError(
            "prepared search reranker injection differs from execution; prepare the query again"
        )
    query, limit, rrf_k = prepared.query, prepared.limit, prepared.rrf_k
    roots = list(prepared.stores)
    for root in roots:
        if root.resolve() != root:
            raise IndexError(f"prepared store no longer resolves to the same directory: {root}")
    try:
        with ExitStack() as stack:
            usage = stack.enter_context(_tracked_vector_usage())
            admission = _admit_stores(
                roots,
                mode=prepared.mode,
                pool=prepared.pool,
                rerank_enabled=prepared.rerank_enabled,
                injected_reranker=prepared.injected_reranker,
                for_query_embedding=False,
                embedders=None,
                stack=stack,
            )
            for before, after in zip(prepared.bindings, admission.bindings, strict=True):
                axis = _changed_axis(before, after)
                if axis is not None:
                    raise IndexError(
                        f"prepared search binding changed for store {after.store} ({axis}); "
                        "prepare the query again"
                    )
            served = {identity: _PreparedVectors(dict(rows)) for identity, rows in prepared.vectors}
            expander = (
                _PreparedExpansion(prepared)
                if prepared.hyde or prepared.multi_query is not None
                else None
            )
            identities = admission.identities

            union: dict[str, StoreHit] = {}
            candidates: dict[str, RerankCandidate] = {}
            for root, conn in admission.connections.items():
                remote: dict[str, VectorMatch] = {}
                hits = _fuse_variants(
                    conn,
                    query=query,
                    mode=prepared.mode,
                    limit=prepared.rerank_in,
                    pool=prepared.pool,
                    rrf_k=rrf_k,
                    embedder=served[identities[root]] if root in identities else None,
                    expander=expander,
                    hyde=prepared.hyde,
                    multi_query=prepared.multi_query,
                    vector_backend=admission.backends.get(root),
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
            if not prepared.rerank_enabled:
                return MultiStoreSearch(
                    [union[key] for key in fused[:limit]],
                    "disabled",
                    vector_usage=_served_usage(usage),
                )
            reranker_config = admission.reranker_config
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
                if prepared.strict:
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
    providers are caller-owned and are never substituted or silently selected. This
    is exactly :func:`prepare_store_search` followed by :func:`execute_prepared`.
    """
    prepared = prepare_store_search(
        query=query,
        stores=stores,
        mode=mode,
        limit=limit,
        pool=pool,
        rrf_k=rrf_k,
        rerank_in=rerank_in,
        rerank_enabled=rerank_enabled,
        strict=strict,
        hyde=hyde,
        multi_query=multi_query,
        embedders=embedders,
        expander=expander,
        injected_reranker=reranker is not None,
    )
    return execute_prepared(prepared, reranker=reranker)


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
