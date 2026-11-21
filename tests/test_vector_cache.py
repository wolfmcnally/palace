"""Proofs of the opt-in exact in-memory vector cache (Phase 35).

The reference for every answer is the released path: the same prepared search executed
without a cache, or the released SQL run on the very connection and snapshot the cache
served. A cached answer must equal it in ids, order, distances, fused scores, rerank and
evidence, through every write, eviction, reload, compaction and failure.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import sqlite_vec

import palace.index.vector_feed as feed_module
import palace.multistore as multistore_module
import palace.search as search_module
import palace.vector_cache as cache_module
from palace.index._errors import IndexError as PalaceError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.embedder_config import resolve_identity
from palace.index.schema import init_db
from palace.index.store_ops import _open, copy_paths
from palace.index.vector_feed import (
    FEED_STATE_TABLE,
    FEED_TABLE,
    VectorFeedDiverged,
    enable_vector_feed,
)
from palace.metadata import DocumentMetadata, MetadataFilter, metadata_connection, replace_documents
from palace.multistore import execute_prepared, prepare_store_search
from palace.search import (
    _open_chunks_db_ro,
    _vector_search,
    execute_prepared_search,
    prepare_search,
)
from palace.vector_cache import VectorCache, VectorCacheUnavailable
from palace.watch.config import WatchRootsConfig, default_config_path
from tests.index.conftest import FakeEmbedder, FakeS3Vectors

REPO = Path(__file__).resolve().parents[1]
WORDS = (
    "lantern",
    "harbor",
    "meadow",
    "copper",
    "kettle",
    "garden",
    "quiet",
    "path",
    "river",
    "stone",
    "orchard",
    "quarry",
    "ember",
    "willow",
    "signal",
    "beacon",
)
TWIN = np.full(EMBED_DIM, 0.25, dtype=np.float32)


class ShapedEmbedder(FakeEmbedder):
    """Content-hashed vectors, except marked texts get exact or near duplicates of TWIN.

    ``twin`` gives TWIN exactly; ``nearN`` gives TWIN with its first component raised by
    N**2 / 100, so near duplicates differ by far less than the cut's error bound but never
    tie; ``query-twin`` gives TWIN with one component lowered, so the twins tie.
    """

    def _vector_for(self, text: str) -> list[float]:
        for token in text.split():
            if token == "twin":
                return [float(value) for value in TWIN]
            if token.startswith("near") and token[4:].isdigit():
                vector = TWIN.copy()
                vector[0] += np.float32(int(token[4:]) ** 2 * 1e-2)
                return [float(value) for value in vector]
            if token == "query-twin":
                vector = TWIN.copy()
                vector[7] -= np.float32(0.5)
                return [float(value) for value in vector]
            if token in ("tiny", "huge", "query-huge"):
                # A subnormal component, or one past the proved range.
                vector = TWIN.copy()
                vector[3] = np.float32(1e-40) if token == "tiny" else np.float32(1e13)
                return [float(value) for value in vector]
        return super()._vector_for(text)


class OrderReranker:
    """Reverses the fused order by scoring later candidates higher; records invocation."""

    def __init__(self, on_score: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self.on_score = on_score

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls += 1
        if self.on_score is not None:
            self.on_score()
        return [float(index) for index in range(len(texts))]

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("PALACE_STORE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def _note(index: int) -> str:
    return f"# Note {index}\n\n{WORDS[index % 16]} {WORDS[(index * 5) % 16]} entry {index}\n"


def _store(
    tmp_path: Path,
    name: str,
    count: int = 40,
    *,
    enable: bool = True,
    extra: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    for index in range(count):
        (root / f"n{index:03d}.md").write_text(_note(index), encoding="utf-8")
    for file_name, body in (extra or {}).items():
        (root / file_name).write_text(body, encoding="utf-8")
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    build(store=store, watch_root_filter=root, full=True, embedder=ShapedEmbedder())
    if enable:
        with closing(_open(store)) as conn:
            enable_vector_feed(conn)
    return store.resolve(), root


def _update(store: Path, root: Path) -> None:
    build(store=store, watch_root_filter=root, full=False, embedder=ShapedEmbedder())


def _cache(**overrides: Any) -> VectorCache:
    settings: dict[str, Any] = {
        "budget_bytes": 64 * 2**20,
        "on_unavailable": "fallback",
        "block_rows": 4,
    }
    settings.update(overrides)
    return VectorCache(**settings)


def _prepared(store: Path, query: str, **overrides: Any) -> search_module.PreparedSearch:
    settings: dict[str, Any] = {
        "query": query,
        "store": store,
        "mode": "hybrid",
        "rerank_enabled": False,
        "embedder": ShapedEmbedder(),
        "pool": 8,
        "limit": 6,
        "evidence": True,
    }
    settings.update(overrides)
    return prepare_search(**settings)


def _same(
    prepared: Any, cache: VectorCache, *, reranker: Callable[[], Any] | None = None
) -> tuple[Any, ...]:
    """The cached result equals the released one; returns the cache outcomes."""
    multi = isinstance(prepared, multistore_module.PreparedStoreSearch)
    run = execute_prepared if multi else execute_prepared_search
    released = run(prepared, reranker=reranker() if reranker else None)
    cached = run(prepared, reranker=reranker() if reranker else None, vector_cache=cache)
    assert cached.hits == released.hits
    assert (cached.status, cached.detail, cached.evidence) == (
        released.status,
        released.detail,
        released.evidence,
    )
    assert released.vector_cache is None and cached.vector_cache is not None
    return cached.vector_cache


def _served(outcomes: Sequence[Any]) -> bool:
    return all(
        item.bypassed or (item.cached_legs > 0 and not item.oracle_legs and not item.fallback_legs)
        for item in outcomes
    )


def _tag(store: Path, root: Path, paths: Sequence[str]) -> list[MetadataFilter]:
    with metadata_connection(chunks_db_path(store), writable=True) as conn:
        replace_documents(
            conn,
            [
                DocumentMetadata(f"doc-{path}", {"form": ["kept"]}, str(root), path)
                for path in paths
            ],
        )
    return [MetadataFilter("form", any_of=["kept"])]


@contextmanager
def _probe_conn(store: Path) -> Iterator[sqlite3.Connection]:
    with closing(_open_chunks_db_ro(chunks_db_path(store))) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM chunks").fetchone()
        yield conn


def _synthetic(tmp_path: Path, name: str, count: int, rng: np.random.Generator) -> Path:
    """A store of ``count`` random vectors written through a feed-aware writer."""
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    chunks_db_path(store).parent.mkdir(parents=True)
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        init_db(conn, store=store)
    with closing(_open(store)) as conn:
        enable_vector_feed(conn)
    _feed_write(store, inserts=_random_rows(rng, name, 0, count))
    return store.resolve()


def _random_rows(
    rng: np.random.Generator, prefix: str, start: int, count: int
) -> list[tuple[str, bytes]]:
    vectors = rng.standard_normal((count, EMBED_DIM)).astype(np.float32)
    return [(f"{prefix}-{start + index:06d}", vectors[index].tobytes()) for index in range(count)]


def _feed_write(
    store: Path, *, inserts: Sequence[tuple[str, bytes]] = (), deletes: Sequence[str] = ()
) -> None:
    """One transaction that writes chunks and vectors the way a feed-aware writer does."""
    root = str(store.parent / f"{store.name}-watch")
    with closing(_open(store)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for chunk_id in deletes:
            if conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,)).rowcount:
                feed_module.record_vector_delete(conn, chunk_id)
            removed = conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (chunk_id,)).rowcount
            feed_module.record_feed_chunk_rows(conn, deleted=removed)
        for chunk_id, embedding in inserts:
            conn.execute(
                "INSERT INTO chunks (chunk_id, path, watch_root, kind, section_index, "
                "window_index, heading_depth, body, body_hash, wikilinks_json, tags_json, "
                "dataview_fields_json, embeds_json, ingest_time) VALUES "
                "(?, ?, ?, 'markdown', 0, 0, 0, 'synthetic', 'h', '[]', '[]', '{}', '[]', 't')",
                (chunk_id, f"{chunk_id}.md", root),
            )
            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)", (chunk_id, embedding)
            )
            feed_module.record_feed_chunk_rows(conn, inserted=1)
            feed_module.record_vector_insert(conn, chunk_id, embedding)
        conn.execute("COMMIT")


def _synthetic_query(store: Path, query: str, pool: int = 8) -> search_module.PreparedSearch:
    return _prepared(store, query, mode="vector", pool=pool, evidence=False)


def _probe(cache: VectorCache, store: Path, query: str, pool: int = 8) -> tuple[Any, Any]:
    """Cached and released vector legs on one connection and snapshot."""
    with cache.lease([store]) as lease, _probe_conn(store) as conn:
        handle = lease.handle(store)
        cached = _vector_search(conn, query, pool, ShapedEmbedder(), vector_cache=handle)
        released = _vector_search(conn, query, pool, ShapedEmbedder())
        return cached, released


def test_cached_vector_hybrid_and_reranked_results_equal_the_released_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path, "one")
    other, other_root = _store(tmp_path, "two", 30)
    cache = _cache()
    for mode in ("vector", "hybrid"):
        for query in ("lantern harbor", "copper ember entry", "willow"):
            assert _served(_same(_prepared(store, query, mode=mode), cache))
    # Hybrid where the keyword and vector legs disagree: both contribute hits.
    prepared = _prepared(store, "quarry", pool=5, limit=10)
    released = execute_prepared_search(prepared)
    assert {hit.bm25_rank is None for hit in released.hits} == {True, False}
    assert _served(_same(prepared, cache))

    # Filters at several selectivities, with evidence.
    for paths in (["n001.md"], [f"n{index:03d}.md" for index in range(0, 40, 3)]):
        filters = _tag(store, root, paths)
        for mode in ("vector", "hybrid"):
            assert _served(_same(_prepared(store, "lantern", mode=mode, filters=filters), cache))

    # Reranked: a reranker that changes the order runs on the cached candidates.
    rerankers: list[OrderReranker] = []

    def reranker() -> OrderReranker:
        rerankers.append(OrderReranker())
        return rerankers[-1]

    prepared = _prepared(store, "garden path", rerank_enabled=True, injected_reranker=True)
    outcomes = _same(prepared, cache, reranker=reranker)
    assert _served(outcomes) and [item.calls for item in rerankers] == [1, 1]
    reranked = execute_prepared_search(prepared, reranker=OrderReranker())
    assert reranked.status == "applied" and reranked.hits[0].rerank_rank == 1

    # Two stores, unreranked and reranked, the same through the multi-store seam.
    identity = resolve_identity(store)
    for rerank in (False, True):
        multi = prepare_store_search(
            query="lantern meadow",
            stores=[store, other],
            mode="hybrid",
            pool=8,
            limit=8,
            rerank_enabled=rerank,
            injected_reranker=rerank,
            embedders={identity: ShapedEmbedder()},
            evidence=True,
        )
        outcomes = _same(multi, cache, reranker=reranker if rerank else None)
        assert [item.store for item in outcomes] == [store, other] and _served(outcomes)
    assert {item.store for item in cache.stats().stores} == {store, other}


def test_ties_rank_by_chunk_id_in_memory_and_in_sql_without_the_released_fallback(
    tmp_path: Path,
) -> None:
    """Equal distances order by chunk id in both paths, inside k and across the cut."""
    extra = {f"t{index}.md": "# Twin\n\ntwin\n" for index in range(5)}
    extra.update({f"near{index}.md": f"# Near\n\nnear{index}\n" for index in range(1, 4)})
    store, _root = _store(tmp_path, "store", 20, extra=extra)
    query = np.asarray(ShapedEmbedder()._vector_for("query-twin"), dtype=np.float32)
    with _probe_conn(store) as conn:
        everything = sorted(
            (float(distance), str(chunk_id))
            for chunk_id, distance in conn.execute(
                "SELECT chunk_id, vec_distance_l2(embedding, ?) FROM chunks_vec",
                (query.tobytes(),),
            )
        )
    twins = [chunk for distance, chunk in everything if distance == everything[0][0]]
    assert len(twins) == 5 and [chunk for _d, chunk in everything[:5]] == twins
    cache = _cache(block_rows=3)
    for pool in (2, 4, 5, 6, 9):
        expected = [chunk for _distance, chunk in everything[:pool]]
        prepared = _prepared(store, "query-twin", mode="vector", pool=pool, limit=pool)
        released = execute_prepared_search(prepared)
        assert [hit.chunk_id for hit in released.hits] == expected, pool
        for _repeat in range(2):
            cached = execute_prepared_search(prepared, vector_cache=cache)
            assert cached.hits == released.hits, pool
            (outcome,) = cached.vector_cache
            assert (outcome.cached_legs, outcome.oracle_legs, outcome.fallback_legs) == (1, 0, 0)
            assert outcome.reasons == ()
    assert not any(name.startswith(("oracle.", "fallbacks.")) for name in cache.stats().counters)


def test_every_cut_is_the_released_answer_or_the_counted_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = {f"t{index}.md": "# Twin\n\ntwin\n" for index in range(3)}
    extra.update({f"near{index}.md": f"# Near\n\nnear{index}\n" for index in range(1, 6)})
    store, root = _store(tmp_path, "store", 30, extra=extra)
    cache = _cache(block_rows=3)

    # Exact ties across block boundaries are ranked from memory and equal the released answer.
    outcome = _same(_prepared(store, "query-twin", mode="vector", pool=6), cache)[0]
    assert outcome.cached_legs == 1 and not outcome.oracle_legs and outcome.reasons == ()
    # A tie crossing the cut is ranked from memory too.
    outcome = _same(_prepared(store, "query-twin", mode="vector", pool=2), cache)[0]
    assert outcome.cached_legs == 1 and not outcome.oracle_legs and outcome.reasons == ()

    # A margin of one forces the certificate to widen; answers stay the released ones.
    monkeypatch.setattr(cache_module, "_initial_margin", lambda _pool: 1)
    widened = cache.stats().counters.get("cut.widened", 0)
    for query in ("lantern", "near3", "copper entry"):
        outcome = _same(_prepared(store, query, mode="vector", pool=4), cache)[0]
        assert outcome.fallback_legs == 0 and outcome.reasons == ()
    assert cache.stats().counters["cut.widened"] > widened

    # Reused slots, a reload and compaction keep the answers.
    for index in range(0, 30, 2):
        (root / f"n{index:03d}.md").unlink()
    _update(store, root)
    for index in range(6):
        (root / f"m{index}.md").write_text(_note(index + 50), encoding="utf-8")
    _update(store, root)
    for query in ("lantern", "ember willow"):
        assert _served(_same(_prepared(store, query, mode="vector", pool=4), cache))
    _same(_prepared(store, "near2", mode="vector", pool=4), cache)
    assert cache.stats().counters.get("compactions", 0) >= 1
    cache.evict(store)
    assert _served(_same(_prepared(store, "lantern", mode="vector", pool=4), cache))
    monkeypatch.undo()

    # A chunk without a vector: inside the filter the oracle reproduces the released
    # (legacy, empty) answer; outside it the cache serves.
    gap, gap_root = _store(tmp_path, "gap", 6, enable=False)
    holed = tmp_path / "holed"
    holed.mkdir()
    WatchRootsConfig().with_added(gap_root, store_root=holed).save(default_config_path(holed))
    chunks_db_path(holed).parent.mkdir(parents=True)
    with closing(sqlite3.connect(chunks_db_path(holed))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        init_db(conn, store=holed)
    with closing(_open(holed)) as conn:
        enable_vector_feed(conn)
    with closing(sqlite3.connect(chunks_db_path(gap))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        missing = conn.execute("SELECT chunk_id FROM chunks WHERE path = 'n001.md'").fetchone()[0]
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (missing,))
        conn.commit()
    copy_paths(
        source=gap, destination=holed, watch_root=gap_root, paths=[f"n{i:03d}.md" for i in range(6)]
    )
    holed = holed.resolve()
    inside = _tag(holed, gap_root, ["n001.md", "n002.md"])
    outcome = _same(_prepared(holed, "lantern", mode="vector", filters=inside), cache)[0]
    assert outcome.reasons == ("missing-vector",) and outcome.oracle_legs == 1
    assert (
        execute_prepared_search(_prepared(holed, "lantern", mode="vector", filters=inside)).hits
        == []
    )
    with metadata_connection(chunks_db_path(holed), writable=True) as conn:
        replace_documents(conn, [], delete_ids=["doc-n001.md"])
    outcome = _same(_prepared(holed, "lantern", mode="vector", filters=inside), cache)[0]
    assert _served([outcome])

    # Outside the range the error bound is proved for (a subnormal stored component, a
    # query component past 2**40), the released SQL answers, with parity.
    numeric, _numeric_root = _store(tmp_path, "numeric", 8, extra={"tiny.md": "# Tiny\n\ntiny\n"})
    outcome = _same(_prepared(numeric, "lantern", mode="vector"), cache)[0]
    assert outcome.reasons == ("numeric-range",) and outcome.oracle_legs == 1
    outcome = _same(_prepared(store, "query-huge", mode="vector"), cache)[0]
    assert outcome.reasons == ("numeric-range",) and outcome.oracle_legs == 1

    # A pool above sqlite-vec's KNN limit: the released refusal, through the oracle.
    prepared = _prepared(store, "lantern", mode="vector", pool=5000)
    with pytest.raises(PalaceError) as released:
        execute_prepared_search(prepared)
    with pytest.raises(PalaceError) as cached:
        execute_prepared_search(prepared, vector_cache=cache)
    assert str(cached.value) == str(released.value)
    assert cache.stats().counters["oracle.knn-limit"] == 1


def test_each_execution_answers_from_its_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path, "store")
    other, other_root = _store(tmp_path, "other", 20)
    cache = _cache()
    burst = "# Burst\n\ntwin\n"
    before = execute_prepared_search(_prepared(store, "query-twin", mode="vector"))
    assert _served(_same(_prepared(store, "query-twin", mode="vector"), cache))

    # A commit between admission and retrieval is not seen; the next query sees it.
    real_admit = search_module._admit_store
    prepared = _prepared(store, "query-twin", mode="vector")

    @contextmanager
    def admitted(**kwargs: Any) -> Iterator[Any]:
        with real_admit(**kwargs) as value:
            (root / "burst.md").write_text(burst, encoding="utf-8")
            _update(store, root)
            yield value

    monkeypatch.setattr(search_module, "_admit_store", admitted)
    pinned = execute_prepared_search(prepared, vector_cache=cache)
    monkeypatch.undo()
    assert pinned.hits == before.hits and _served(pinned.vector_cache or ())
    after = _same(_prepared(store, "query-twin", mode="vector"), cache)
    assert _served(after)
    assert (
        execute_prepared_search(_prepared(store, "query-twin", mode="vector")).hits != before.hits
    )

    # The same for the second of two stores admitted together.
    identity = resolve_identity(store)

    def multi() -> Any:
        return prepare_store_search(
            query="query-twin",
            stores=[store, other],
            mode="vector",
            pool=8,
            limit=8,
            rerank_enabled=False,
            embedders={identity: ShapedEmbedder()},
        )

    before_multi = execute_prepared(multi())
    real_admit_stores = multistore_module._admit_stores

    def admitted_stores(*args: Any, **kwargs: Any) -> Any:
        admission = real_admit_stores(*args, **kwargs)
        (other_root / "burst.md").write_text("# Burst\n\ntwin\n", encoding="utf-8")
        _update(other, other_root)
        return admission

    prepared_multi = multi()
    monkeypatch.setattr(multistore_module, "_admit_stores", admitted_stores)
    pinned_multi = execute_prepared(prepared_multi, vector_cache=cache)
    monkeypatch.undo()
    assert pinned_multi.hits == before_multi.hits and _served(pinned_multi.vector_cache or ())
    assert _served(_same(multi(), cache))
    assert execute_prepared(multi()).hits != before_multi.hits

    # A rerank during which another commit lands still reranks the pinned candidates.
    prepared = _prepared(store, "harbor", rerank_enabled=True, injected_reranker=True)
    expected = execute_prepared_search(prepared, reranker=OrderReranker())

    def commit() -> None:
        (root / "late.md").write_text("# Late\n\nharbor harbor harbor\n", encoding="utf-8")
        _update(store, root)

    during = execute_prepared_search(
        prepared, reranker=OrderReranker(on_score=commit), vector_cache=cache
    )
    assert during.hits == expected.hits
    assert _served(_same(prepared, cache, reranker=OrderReranker))

    # The cached single-store path holds its admitted snapshot through the rerank, on a
    # hit and on a fallback; without a cache the released lifetime is unchanged.
    events: list[str] = []
    real_admit = search_module._admit_store

    @contextmanager
    def watched(**kwargs: Any) -> Iterator[Any]:
        with real_admit(**kwargs) as value:
            events.append("admitted")
            yield value
        events.append("closed")

    lifetime = _prepared(store, "harbor", rerank_enabled=True, injected_reranker=True)
    monkeypatch.setattr(search_module, "_admit_store", watched)
    for label, chosen in (("hit", cache), ("fallback", _cache(budget_bytes=1024)), ("none", None)):
        events.clear()
        result = execute_prepared_search(
            lifetime,
            reranker=OrderReranker(on_score=lambda: events.append("rerank")),
            vector_cache=chosen,
        )
        order = (
            ["admitted", "closed", "rerank"] if chosen is None else ["admitted", "rerank", "closed"]
        )
        assert events == order, label
        if chosen is not None:
            outcome = (result.vector_cache or ())[0]
            assert (outcome.cached_legs, outcome.fallback_legs) == (
                (1, 0) if label == "hit" else (0, 1)
            )
    monkeypatch.undo()

    # An older lease keeps its view through a newer cold load, a prune and a compaction.
    monkeypatch.setattr("palace.index.vector_feed.VECTOR_FEED_TAIL_ROWS", 2)
    with cache.lease([store]) as lease, _probe_conn(store) as conn:
        handle = lease.handle(store)
        first = _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=handle)
        for index in range(0, 40, 2):
            (root / f"n{index:03d}.md").unlink()
        _update(store, root)
        cache.evict(store)
        cached, released = _probe(cache, store, "lantern")
        assert cached == released
        for index in range(1, 40, 4):
            (root / f"n{index:03d}.md").unlink()
        _update(store, root)
        cached, released = _probe(cache, store, "lantern")
        assert cached == released
        again = _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=handle)
        assert first == again == _vector_search(conn, "lantern", 8, ShapedEmbedder())
        assert handle is not None and handle.cached_legs == 2
    counters = cache.stats().counters
    assert counters["evictions.operator"] >= 1 and counters["loads"] >= 2


def test_concurrent_readers_never_see_a_stale_or_torn_answer(tmp_path: Path) -> None:
    store, root = _store(tmp_path, "store", 24)
    source, _source_root = _store(tmp_path, "source", 0, enable=False)
    shutil_copy = [f"n{index:03d}.md" for index in range(4)]
    cache = _cache(block_rows=5)
    stop = threading.Event()
    errors: list[BaseException] = []
    served = {"legs": 0}
    lock = threading.Lock()

    def writer() -> None:
        try:
            for round_index in range(12):
                (root / f"w{round_index}.md").write_text(_note(round_index + 100), encoding="utf-8")
                if round_index % 3 == 1:
                    (root / f"n{round_index:03d}.md").unlink()
                if round_index % 4 == 2:
                    (root / f"n{round_index + 10:03d}.md").write_text(
                        _note(round_index + 200), encoding="utf-8"
                    )
                _update(store, root)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            stop.set()

    def reader(query: str) -> None:
        try:
            while not stop.is_set():
                with cache.lease([store]) as lease, _probe_conn(store) as conn:
                    handle = lease.handle(store)
                    for _ in range(2):
                        cached = _vector_search(
                            conn, query, 6, ShapedEmbedder(), vector_cache=handle
                        )
                        assert cached == _vector_search(conn, query, 6, ShapedEmbedder())
                    assert handle is not None
                    with lock:
                        served["legs"] += handle.cached_legs
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader, args=(query,))
        for query in ("lantern", "ember", "river path")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors, errors
    assert served["legs"] > 0
    del source, shutil_copy


def test_memory_is_ledgered_appended_reused_zeroed_and_released_after_the_last_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path, "store", 20)
    other, _other_root = _store(tmp_path, "other", 20)
    cache = _cache(block_rows=4)
    block = cache._block_bytes(EMBED_DIM)
    created: list[int] = []
    real_init = cache_module._Block.__init__

    def counted(self: Any, rows: int, dim: int) -> None:
        created.append(rows)
        real_init(self, rows, dim)

    monkeypatch.setattr(cache_module._Block, "__init__", counted)
    peaks: list[int] = []
    for name in ("_take_blocks", "_reserve_scratch"):
        real = getattr(VectorCache, name)

        def checked(self: VectorCache, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            result = _real(self, *args, **kwargs)
            owned = sum(self._bytes.values())
            peaks.append(owned)
            assert owned <= self.budget_bytes
            return result

        monkeypatch.setattr(VectorCache, name, checked)

    assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
    acc = cache._entries[store].acc
    assert acc is not None
    arrays = [id(item.data) for item in acc.blocks]
    loaded_blocks = len(created)
    # An ordinary update appends: no existing block is copied or replaced.
    for index in range(3):
        (root / f"add{index}.md").write_text(_note(index + 70), encoding="utf-8")
    _update(store, root)
    assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
    assert [id(item.data) for item in acc.blocks[: len(arrays)]] == arrays
    assert len(created) - loaded_blocks <= 1

    # Excision unpublishes now and frees memory after the last lease, into a zeroed pool.
    with cache.lease([store]) as lease, _probe_conn(store) as conn:
        _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=lease.handle(store))
        assert cache.evict(store)
        stats = cache.stats()
        assert store not in {item.store for item in stats.stores}
        held_blocks = len(acc.blocks) + len(acc.retired)
        assert stats.retired_bytes == held_blocks * block > 0 and stats.live_bytes == 0
    stats = cache.stats()
    assert stats.retired_bytes == 0 and stats.pooled_bytes > 0
    for pooled in cache._pool[EMBED_DIM]:
        assert not pooled.data.any() and pooled.ids == [None] * pooled.capacity
        assert pooled.used == 0 and not pooled.norm2.any()
    # The pool is reused before anything new is allocated.
    created.clear()
    assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
    assert created == [] and cache.stats().pooled_bytes < stats.pooled_bytes

    # Under a budget that holds one store, a pinned store makes the other unavailable.
    # Room for one 32-row block, a load's check buffers and a query's scratch, never two
    # resident blocks.
    roomy = _cache(block_rows=32)
    check = cache_module._CHECK_ROWS * EMBED_DIM * 8
    tight = _cache(budget_bytes=2 * roomy._block_bytes(EMBED_DIM) + check - 1, block_rows=32)
    for setting in ("fallback", "refuse"):
        tight.on_unavailable = setting
        with tight.lease([store]) as lease, _probe_conn(store) as conn:
            _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=lease.handle(store))
            if setting == "fallback":
                outcome = _same(_prepared(other, "lantern", mode="vector"), tight)[0]
                assert outcome.fallback_legs == 1 and outcome.reasons == ("budget",)
            else:
                with pytest.raises(VectorCacheUnavailable) as refused:
                    execute_prepared_search(
                        _prepared(other, "lantern", mode="vector"), vector_cache=tight
                    )
                assert refused.value.reason == "budget"
    counters = tight.stats().counters
    assert counters["reservation_refusals"] >= 2 and counters["fallbacks.budget"] == 1
    # Unpinned, pressure evicts the least recently used store to make room.
    assert _served(_same(_prepared(other, "lantern", mode="vector"), tight))
    assert tight.stats().counters["evictions.pressure"] >= 1
    assert max(peaks) <= cache.budget_bytes

    # An eviction while one lease has pinned but not yet viewed the store, and another
    # lease that had a view then ends: no block of the store reaches the pool until the
    # last lease ends, and the pinned lease answers by fallback, never from freed memory.
    shared = _cache(block_rows=4)
    assert shared.warm(store)
    with shared.lease([store]) as first:
        with shared.lease([store]) as second, _probe_conn(store) as conn:
            _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=second.handle(store))
            evicted = shared._entries[store].acc
            assert evicted is not None and shared.evict(store)
        pooled_now = {id(item) for item in shared._pool.get(EMBED_DIM, [])}
        assert not pooled_now & {id(item) for item in evicted.blocks}
        with _probe_conn(store) as conn:
            cached = _vector_search(
                conn, "lantern", 8, ShapedEmbedder(), vector_cache=first.handle(store)
            )
            assert cached == _vector_search(conn, "lantern", 8, ShapedEmbedder())
        handle = first.handle(store)
        assert handle is not None and handle.reasons == {"snapshot"}
    stats = shared.stats()
    assert stats.retired_bytes == 0 and stats.live_bytes == 0 and stats.pooled_bytes > 0

    # Working memory, growth, outstanding replacements and pressure reclamation, measured
    # on a larger synthetic store with the wrappers above removed.
    monkeypatch.undo()
    working = tmp_path / "working"
    working.mkdir()
    _working_memory_is_reserved_before_it_is_allocated(working, monkeypatch)


def _traced(action: Callable[[], object], cache: VectorCache, ledger: list[int]) -> tuple[int, int]:
    """Peak bytes Python and numpy allocated during ``action``, and the ledger's peak rise."""
    ledger.clear()
    before = sum(cache._bytes.values())
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        action()
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    return peak, max([before, *ledger]) - before


def _working_memory_is_reserved_before_it_is_allocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = np.random.default_rng(35)
    store = _synthetic(tmp_path, "synthetic", 2500, rng)
    cache = _cache(budget_bytes=512 * 2**20, block_rows=1024)
    ledger: list[int] = []
    for name in ("_take_blocks", "_reserve_scratch"):
        real = getattr(VectorCache, name)

        def recorded(self: VectorCache, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            result = _real(self, *args, **kwargs)
            ledger.append(sum(self._bytes.values()))
            return result

        monkeypatch.setattr(VectorCache, name, recorded)
    slack = 2**20
    prepared = _synthetic_query(store, "lantern")
    # Allocation measured independently of the ledger stays within what it reserved.
    for label, action in (
        ("load", lambda: cache.warm(store)),
        ("query", lambda: execute_prepared_search(prepared, vector_cache=cache)),
    ):
        peak, reserved = _traced(action, cache, ledger)
        assert peak <= reserved + slack, (label, peak, reserved)
    acc = cache._entries[store].acc
    assert acc is not None
    deleted = [f"synthetic-{index:06d}" for index in range(0, 600)]
    _feed_write(store, deletes=deleted, inserts=_random_rows(rng, "added", 0, 100))
    peak, reserved = _traced(
        lambda: execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache),
        cache,
        ledger,
    )
    assert cache.stats().counters["compactions"] == 1
    assert peak <= reserved + slack, ("refresh and compaction", peak, reserved)
    released = execute_prepared_search(_synthetic_query(store, "lantern"))
    assert (
        execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache).hits
        == released.hits
    )

    # Growth counts every free slot: filling exactly the free room makes no block.
    created: list[int] = []
    real_init = cache_module._Block.__init__

    def counted(self: Any, rows: int, dim: int) -> None:
        created.append(rows)
        real_init(self, rows, dim)

    monkeypatch.setattr(cache_module._Block, "__init__", counted)
    pooled = len(cache._pool.get(EMBED_DIM, []))
    free = sum(block.capacity - block.used for block in acc.blocks)
    tail_free = acc.blocks[-1].capacity - acc.blocks[-1].used
    assert free > tail_free > 0
    _feed_write(store, inserts=_random_rows(rng, "filled", 0, free))
    execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache)
    assert created == [] and len(cache._pool.get(EMBED_DIM, [])) == pooled
    blocks = len(acc.blocks)
    _feed_write(store, inserts=_random_rows(rng, "overflow", 0, 1))
    execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache)
    # One more row than the free room takes exactly one block, from the pool if it can.
    assert len(acc.blocks) == blocks + 1
    assert len(created) + (pooled - len(cache._pool.get(EMBED_DIM, []))) == 1

    # One outstanding replacement per store. Rows deleted before an older view's
    # snapshot can be dropped while it is held, so a compaction replaces their block and
    # the view keeps the replaced one; until that view ends, no further compaction runs.
    def ids(start: int, stop: int) -> list[str]:
        return [f"synthetic-{index:06d}" for index in range(start, stop)]

    def query() -> None:
        execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache)

    _feed_write(store, deletes=ids(1024, 1224) + ids(2048, 2300))
    query()
    assert cache.stats().counters["compactions"] == 1
    with cache.lease([store]) as older, _probe_conn(store) as conn:
        _vector_search(conn, "lantern", 8, ShapedEmbedder(), vector_cache=older.handle(store))
        _feed_write(store, deletes=ids(1224, 1324))
        query()
        assert len(acc.retired) == 1 and cache.stats().counters["compactions"] == 2
        _feed_write(store, deletes=ids(2300, 2320))
        query()
        assert len(acc.retired) == 1 and cache.stats().counters["compactions"] == 2
    query()
    assert cache.stats().counters["compactions"] == 3 and len(acc.retired) <= 1
    assert _served(_same(_synthetic_query(store, "lantern"), cache))

    # Under reservation pressure with an older view held, rows that view may still read
    # are never overwritten: the leg is unavailable. Once no other lease holds the store,
    # slots of rows no one can see are reused in place and the answer stays exact.
    real_take = VectorCache._take_blocks

    def refused(self: VectorCache, count: int, dim: int, category: str, exclude: Path) -> Any:
        if category == "live" and count:
            return None
        return real_take(self, count, dim, category, exclude)

    monkeypatch.setattr(VectorCache, "_take_blocks", refused)
    _feed_write(store, deletes=ids(1400, 1500))
    execute_prepared_search(_synthetic_query(store, "lantern"), vector_cache=cache)
    room = sum(block.capacity - block.used for block in acc.blocks)
    with cache.lease([store]) as older, _probe_conn(store) as conn:
        first = _vector_search(
            conn, "lantern", 8, ShapedEmbedder(), vector_cache=older.handle(store)
        )
        _feed_write(store, inserts=_random_rows(rng, "pressed", 0, room + 20))
        outcome = _same(_synthetic_query(store, "lantern"), cache)[0]
        assert outcome.reasons == ("budget",) and outcome.fallback_legs == 1
        again = _vector_search(
            conn, "lantern", 8, ShapedEmbedder(), vector_cache=older.handle(store)
        )
        assert first == again == _vector_search(conn, "lantern", 8, ShapedEmbedder())
    assert _served(_same(_synthetic_query(store, "lantern"), cache))
    assert cache.stats().counters["reclaimed_slots"] == 1


def test_loads_are_single_flight_bounded_and_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _root = _store(tmp_path, "store", 12)
    cache = _cache(max_waiting_loads=3)
    release = threading.Event()
    started = threading.Event()
    loads: list[Path] = []
    real_load = VectorCache._load

    def slow(self: VectorCache, target: Path) -> Any:
        loads.append(target)
        started.set()
        release.wait(30)
        return real_load(self, target)

    monkeypatch.setattr(VectorCache, "_load", slow)
    results: list[Any] = []
    errors: list[BaseException] = []

    def query() -> None:
        try:
            results.append(_same(_prepared(store, "lantern", mode="vector"), cache)[0])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=query) for _ in range(5)]
    for thread in threads:
        thread.start()
    assert started.wait(10)
    deadline = time.monotonic() + 10
    while len(errors) + len(results) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for thread in threads:
        thread.join(30)
    assert not errors and len(results) == 5 and len(loads) == 1
    # One loader, three waiters; the fifth execution finds the queue full.
    assert sorted(item.fallback_legs for item in results) == [0, 0, 0, 0, 1]
    assert {item.reasons for item in results if item.fallback_legs} == {("queue-full",)}
    assert cache.stats().counters["loads"] == 1

    # An eviction while a load is outstanding: the load is discarded, never published.
    cache.evict(store)
    release.clear()
    started.clear()
    # A waiter on that load is fenced too: it neither pins nor starts a load of its own.
    outcome: list[Any] = []
    prepared = [_prepared(store, "lantern", mode="vector") for _ in range(2)]
    threads = [
        threading.Thread(target=lambda value=value: outcome.append(_same(value, cache)[0]))
        for value in prepared
    ]
    loads.clear()
    threads[0].start()
    assert started.wait(10)
    threads[1].start()
    deadline = time.monotonic() + 10
    while cache._waiting < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache._waiting == 1
    cache.evict(store)
    release.set()
    for thread in threads:
        thread.join(30)
    assert [item.reasons for item in outcome] == [("snapshot",), ("snapshot",)]
    assert len(loads) == 1
    stats = cache.stats()
    assert stats.stores == () and stats.counters["discarded_loads"] == 1
    assert stats.load_bytes == 0 and stats.live_bytes == 0 and stats.counters["loads"] == 1
    # A later execution loads afresh.
    assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
    assert cache.stats().counters["loads"] == 2


def _corrupt(store: Path, statement: str, parameters: Sequence[Any] = ()) -> None:
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute(statement, parameters)
        conn.commit()


def test_integrity_failures_fail_the_query_under_both_settings_and_evict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path

    def bypass_writer() -> None:
        _corrupt(store, "DELETE FROM chunks WHERE path = 'n003.md'")

    def malformed() -> None:
        _corrupt(store, f"UPDATE {FEED_STATE_TABLE} SET floor_seq = last_seq + 5")

    def wrong_width() -> None:
        _corrupt(
            store,
            f"INSERT INTO {FEED_TABLE} (op, chunk_id, embedding) VALUES ('insert', 'x', ?)",
            (b"\0" * 8,),
        )
        _corrupt(
            store,
            f"UPDATE {FEED_STATE_TABLE} SET last_seq = (SELECT max(seq) FROM {FEED_TABLE}), "
            "vector_rows = vector_rows + 1",
        )

    def miscounted() -> None:
        _corrupt(store, f"UPDATE {FEED_STATE_TABLE} SET vector_rows = vector_rows + 1")

    cases: list[tuple[Callable[[], None], type[BaseException], str, bool]] = [
        (bypass_writer, VectorFeedDiverged, "diverged", True),
        (malformed, PalaceError, "malformed", True),
        (wrong_width, PalaceError, "dimension", True),
        (miscounted, PalaceError, "reconcile", False),
        # The same miscount on a resident store, caught on a hit at the same position.
        (miscounted, PalaceError, "reconcile", True),
    ]
    for setting in ("fallback", "refuse"):
        for damage, error, text, warm in cases:
            store, _root = _store(tmp_path, f"{setting}-{text}-{warm}", 10)
            cache = _cache(on_unavailable=setting)
            if warm:
                assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
            damage()
            with pytest.raises(error, match=text):
                execute_prepared_search(
                    _prepared(store, "lantern", mode="vector"), vector_cache=cache
                )
            stats = cache.stats()
            assert stats.stores == () and stats.live_bytes == 0, (setting, text)
            assert not any(key.startswith(("fallbacks.", "refusals.")) for key in stats.counters)
            if warm:
                assert stats.counters["evictions.integrity"] == 1

    # An operational fallback never answers over a diverged or malformed feed: the
    # admitted snapshot's state is checked on the connection that would answer.
    for damage, error, text in (
        (bypass_writer, VectorFeedDiverged, "diverged"),
        (malformed, PalaceError, "malformed"),
    ):
        store, _root = _store(tmp_path, f"pressure-{text}", 10)
        starved = _cache(budget_bytes=1024)
        prepared = _prepared(store, "lantern", mode="vector")
        real_admit = search_module._admit_store

        @contextmanager
        def damaged(
            *, _damage: Callable[[], None] = damage, _real: Any = real_admit, **kwargs: Any
        ) -> Iterator[Any]:
            # The load saw a healthy feed and was refused for budget; the damage lands
            # after acquisition and before admission, so only the answering snapshot
            # shows it.
            _damage()
            with _real(**kwargs) as value:
                yield value

        monkeypatch.setattr(search_module, "_admit_store", damaged)
        with pytest.raises(error, match=text):
            execute_prepared_search(prepared, vector_cache=starved)
        monkeypatch.undo()
        counters = starved.stats().counters
        assert counters["reservation_refusals"] >= 1 and "fallbacks.budget" not in counters
    healthy, _root = _store(tmp_path, "pressure-healthy", 10)
    outcome = _same(_prepared(healthy, "lantern", mode="vector"), _cache(budget_bytes=1024))[0]
    assert outcome.reasons == ("budget",) and outcome.fallback_legs == 1


def test_authorization_runs_for_every_store_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _first_root = _store(tmp_path, "first", 10)
    second, _second_root = _store(tmp_path, "second", 10)
    events: list[str] = []
    denied: set[Path] = set()

    def authorize(store: Path) -> bool:
        events.append(f"authorize:{store.name}")
        return store not in denied

    cache = _cache(authorize=authorize)
    real_admit = search_module._admit_store

    @contextmanager
    def admitted(**kwargs: Any) -> Iterator[Any]:
        events.append(f"admit:{kwargs['store'].name}")
        with real_admit(**kwargs) as value:
            yield value

    prepared = [_prepared(first, "lantern", mode="vector") for _ in range(2)]
    monkeypatch.setattr(search_module, "_admit_store", admitted)
    for value in prepared:
        execute_prepared_search(value, vector_cache=cache)
    assert events == ["authorize:first", "admit:first"] * 2
    assert cache.stats().counters["loads"] == 1

    # A denial of the second store stops the execution before any store is admitted.
    identity = resolve_identity(first)
    multi = prepare_store_search(
        query="lantern",
        stores=[first, second],
        mode="vector",
        rerank_enabled=False,
        embedders={identity: ShapedEmbedder()},
    )
    admissions: list[Any] = []
    real_admit_stores = multistore_module._admit_stores

    def recorded(*args: Any, **kwargs: Any) -> Any:
        admissions.append(args)
        return real_admit_stores(*args, **kwargs)

    monkeypatch.setattr(multistore_module, "_admit_stores", recorded)
    denied.add(second)
    events.clear()
    with pytest.raises(PalaceError, match="authorization refused"):
        execute_prepared(multi, vector_cache=cache)
    assert admissions == [] and events == ["authorize:first", "authorize:second"]
    # A denied resident store is evicted; warm honours the hook too.
    denied.add(first)
    with pytest.raises(PalaceError, match="authorization refused"):
        execute_prepared_search(prepared[0], vector_cache=cache)
    assert cache.stats().stores == () and cache.stats().counters["evictions.authorization"] == 1
    with pytest.raises(PalaceError, match="authorization refused"):
        cache.warm(second)
    denied.clear()
    assert cache.warm(second) and [item.store for item in cache.stats().stores] == [second]

    def broken(_store: Path) -> bool:
        raise RuntimeError("authorization service down")

    cache.authorize = broken
    with pytest.raises(RuntimeError, match="service down"):
        execute_prepared_search(_prepared(second, "lantern", mode="vector"), vector_cache=cache)
    assert cache.stats().stores == ()

    # One store, any spelling: authorization, residency and eviction all use the resolved
    # directory, so a symlink, a relative path and the real path name one resident copy.
    seen: list[Path] = []

    def recording(store: Path) -> bool:
        seen.append(store)
        return True

    spelled = _cache(authorize=recording)
    link = tmp_path / "first-link"
    link.symlink_to(first)
    assert spelled.warm(link)
    assert [item.store for item in spelled.stats().stores] == [first]
    assert spelled.evict(first) and spelled.stats().stores == ()
    monkeypatch.chdir(tmp_path)
    relative = execute_prepared_search(
        _prepared(Path("first"), "lantern", mode="vector"), vector_cache=spelled
    )
    assert [item.store for item in relative.vector_cache or ()] == [first]
    assert spelled.warm(link) and [item.store for item in spelled.stats().stores] == [first]
    assert spelled.stats().counters["loads"] == 2
    assert spelled.evict(Path("first")) and spelled.stats().stores == ()
    assert seen and set(seen) == {first}


def _resident(cache: VectorCache, store: Path) -> dict[str, bytes]:
    acc = cache._entries[store].acc
    assert acc is not None
    rows: dict[str, bytes] = {}
    for block in acc.blocks:
        for slot in range(block.used):
            if block.dead[slot] == cache_module._LIVE:
                chunk_id = block.ids[slot]
                assert chunk_id is not None
                rows[chunk_id] = block.data[slot].tobytes()
    return rows


def test_fast_and_supported_loaders_agree_and_the_fast_one_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path, "store", 14)
    for index in (2, 5, 6, 11):
        (root / f"n{index:03d}.md").unlink()
    _update(store, root)
    for index in range(3):
        (root / f"r{index}.md").write_text(_note(index + 30), encoding="utf-8")
    _update(store, root)
    empty_root = tmp_path / "empty-watch"
    empty_root.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    WatchRootsConfig().with_added(empty_root, store_root=empty).save(default_config_path(empty))
    chunks_db_path(empty).parent.mkdir(parents=True)
    with closing(sqlite3.connect(chunks_db_path(empty))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        init_db(conn, store=empty)
    with closing(_open(empty)) as conn:
        enable_vector_feed(conn)
    empty = empty.resolve()
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        stored = {
            str(chunk_id): bytes(embedding)
            for chunk_id, embedding in conn.execute("SELECT chunk_id, embedding FROM chunks_vec")
        }
    for target, expected in ((store, stored), (empty, {})):
        for block_rows in (3, 4, 64):
            fast = _cache(block_rows=block_rows)
            assert fast.warm(target)
            monkeypatch.setattr(cache_module, "_fast_qualified", lambda _conn: False)
            supported = _cache(block_rows=block_rows)
            assert supported.warm(target)
            monkeypatch.undo()
            assert _resident(fast, target) == _resident(supported, target) == expected
            assert [item.loader_route for item in fast.stats().stores] == ["fast"]
            assert [item.loader_route for item in supported.stats().stores] == ["supported"]
    assert (
        execute_prepared_search(
            _prepared(empty, "lantern", mode="vector"), vector_cache=_cache()
        ).hits
        == []
    )

    # A corrupted location map is caught by the check and the supported route answers.
    monkeypatch.setattr(
        cache_module,
        "_FAST_LOCATIONS_SQL",
        "SELECT id, chunk_id, (chunk_offset + 1) % 1024 FROM chunks_vec_rowids "
        "ORDER BY chunk_id, chunk_offset",
    )
    checked = _cache()
    assert _served(_same(_prepared(store, "lantern", mode="vector"), checked))
    stats = checked.stats()
    assert stats.counters["loader.fast-rejected"] == 1
    assert [item.loader_route for item in stats.stores] == ["supported"]
    assert _resident(checked, store) == stored
    monkeypatch.undo()

    # An unqualified sqlite-vec version uses the supported route and says so.
    monkeypatch.setattr(cache_module, "QUALIFIED_SQLITE_VEC_VERSION", "v0.0.0")
    unqualified = _cache()
    assert unqualified.warm(store)
    assert unqualified.stats().counters["loader.unqualified-version"] == 1
    assert _resident(unqualified, store) == stored


def test_a_fresh_process_loads_the_last_committed_state_after_a_killed_writer(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path, "store", 10)
    marker = tmp_path / "marker"
    child = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "tests" / "vector_cache_restart_child.py"),
            str(store),
            str(root),
            str(marker),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None, "the child exited before its write"
            time.sleep(0.05)
        assert marker.exists()
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(30)
    (root / "interrupted.md").unlink()
    cache = _cache()
    outcome = _same(_prepared(store, "never committed", mode="vector"), cache)
    assert _served(outcome)
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE path = 'interrupted.md'").fetchone()[0]
            == 0
        )
    (root / "later.md").write_text("# Later\n\ntwin\n", encoding="utf-8")
    _update(store, root)
    later = execute_prepared_search(
        _prepared(store, "query-twin", mode="vector"), vector_cache=cache
    )
    assert _served(later.vector_cache or ())
    assert any(hit.path.endswith("later.md") for hit in later.hits)
    assert later.hits == execute_prepared_search(_prepared(store, "query-twin", mode="vector")).hits


def test_a_search_without_a_cache_never_loads_it_and_unserved_legs_are_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _root = _store(tmp_path, "store", 8)
    # A process that searches without a cache, through the library and the command-line
    # entry module, loads neither the cache nor the writer-side feed module.
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "import palace.cli, palace.multistore, palace.search\n"
        "from tests.index.conftest import FakeEmbedder\n"
        f"hits = palace.search.search(query='lantern', store=Path({str(store)!r}), mode='vector',"
        " embedder=FakeEmbedder())\n"
        "assert hits\n"
        "loaded = [name for name in ('palace.vector_cache', 'palace.index.vector_feed')"
        " if name in sys.modules]\n"
        "print(loaded)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "[]"
    imported_before = set(sys.modules)
    assert "palace.vector_cache" in imported_before  # this module imported it, not a search

    cache = _cache()
    outcome = _same(_prepared(store, "lantern", mode="bm25", evidence=False), cache)[0]
    assert outcome.bypassed and outcome.cached_legs == outcome.fallback_legs == 0
    assert cache.stats().stores == ()

    import palace.index.s3vectors as s3v

    published, _published_root = _store(tmp_path, "published", 8, enable=True)
    fake = FakeS3Vectors()
    monkeypatch.setattr(s3v, "open_client", lambda profile, region: fake)
    s3v.save_publishing_config(published, note="fixture corpus, published to a fixture index")
    s3v.publish_vectors(
        store=published,
        bucket="fixture-vectors",
        index="fixture-index",
        region="us-east-1",
        profile="fixture",
        text_metadata=True,
    )
    s3v.save_vector_backend_config(published, s3v.VectorBackendConfig(s3v.BACKEND_S3VECTORS))
    identity = resolve_identity(store)
    prepared = prepare_store_search(
        query="lantern",
        stores=[published, store],
        mode="vector",
        rerank_enabled=False,
        embedders={identity: ShapedEmbedder()},
    )
    outcomes = _same(prepared, cache)
    assert [(item.store, item.bypassed) for item in outcomes] == [(published, True), (store, False)]
    assert [item.store for item in cache.stats().stores] == [store]


def test_stats_reconcile_move_as_predicted_and_cost_the_same_at_any_size(
    tmp_path: Path,
) -> None:
    small, small_root = _store(tmp_path, "small", 6)
    large, _large_root = _store(tmp_path, "large", 60)
    cache = _cache(block_rows=4)
    empty = cache.stats()
    assert (
        empty.owned_bytes == 0
        and empty.headroom_bytes == empty.budget_bytes
        and empty.counters == {}
    )
    for store in (small, large):
        assert _served(_same(_prepared(store, "lantern", mode="vector"), cache))
    block = cache._block_bytes(EMBED_DIM)

    def reconciled(stats: cache_module.VectorCacheStats) -> None:
        parts = (
            stats.live_bytes,
            stats.retired_bytes,
            stats.pooled_bytes,
            stats.load_bytes,
            stats.scratch_bytes,
        )
        assert stats.owned_bytes == sum(parts) and min(parts) >= 0
        assert stats.headroom_bytes == stats.budget_bytes - stats.owned_bytes
        assert (
            sum(item.owned_bytes for item in stats.stores) <= stats.live_bytes + stats.retired_bytes
        )

    stats = cache.stats()
    reconciled(stats)
    by_store = {item.store: item for item in stats.stores}
    assert by_store[small].live_rows == 6 and by_store[large].live_rows == 60
    assert stats.live_bytes == (2 + 15) * block and stats.counters == {"loads": 2}
    assert all(item.applied_seq == item.base_seq for item in stats.stores)

    for index in range(3):
        (small_root / f"n{index:03d}.md").unlink()
    _update(small, small_root)
    with cache.lease([small]) as lease, _probe_conn(small) as conn:
        _vector_search(conn, "lantern", 4, ShapedEmbedder(), vector_cache=lease.handle(small))
        held = cache.stats()
        reconciled(held)
        assert held.leases == 1
        item = {entry.store: entry for entry in held.stores}[small]
        assert item.live_rows == 3 and item.applied_seq > item.base_seq and item.leases == 1

    # Reconciliation holds while another thread refreshes and compacts, and every
    # (position, live rows) pair a reader sees is a committed state of the store.
    def committed_now() -> tuple[int, int]:
        with closing(sqlite3.connect(chunks_db_path(small))) as conn:
            state = feed_module.read_vector_feed_state(conn)
        assert state is not None
        return state.last_seq, state.vector_rows

    committed = dict([committed_now()])
    stop = threading.Event()
    failures: list[BaseException] = []
    observed: set[tuple[int, int]] = set()

    def watch() -> None:
        try:
            while not stop.is_set():
                stats = cache.stats()
                reconciled(stats)
                observed.update(
                    (item.applied_seq, item.live_rows)
                    for item in stats.stores
                    if item.store == small
                )
        except BaseException as exc:
            failures.append(exc)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        for index in range(3, 6):
            (small_root / f"n{index:03d}.md").unlink()
            _update(small, small_root)
            position, rows = committed_now()
            committed[position] = rows
            _same(_prepared(small, "lantern", mode="vector"), cache)
    finally:
        stop.set()
        watcher.join(30)
    assert not failures, failures
    assert observed and all(committed.get(position) == rows for position, rows in observed), (
        observed,
        committed,
    )

    # stats() reads per-store figures only: its call count does not grow with rows.
    def calls(target: VectorCache) -> int:
        count = {"n": 0}

        def profile(_frame: Any, event: str, _arg: Any) -> None:
            if event in ("call", "c_call"):
                count["n"] += 1

        sys.setprofile(profile)
        try:
            target.stats()
        finally:
            sys.setprofile(None)
        return count["n"]

    one_small, one_large = _cache(), _cache()
    one_small.warm(small)
    one_large.warm(large)
    assert calls(one_small) == calls(one_large)
