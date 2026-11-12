"""Proofs of the single-store prepare-and-execute seam in :mod:`palace.search`.

The reference retrieval below is the interleaved core every entry point ran before the
seam: one read snapshot, the live embedder and expander called by ``_search_conn`` or
``_fuse_variants`` during retrieval. Prepared execution must reproduce its whole result
and exactly its provider calls, inputs and order, while never embedding itself.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

import palace.search as search_module
from palace.index._errors import IndexError as SearchError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.embedder_config import (
    EmbedderConfig,
    load_embedder_config,
    resolve_identity,
    save_embedder_config,
)
from palace.index.schema import stamp_identity
from palace.metadata import DocumentMetadata, MetadataFilter, metadata_connection, replace_documents
from palace.rerank import RerankCandidate, RerankUnavailableError, rerank
from palace.rerank_config import RerankConfig, RerankIdentity
from palace.retrieval_config import RERANK_MODEL
from palace.search import (
    PreparedSearch,
    RerankedSearch,
    SearchHit,
    _fuse_variants,
    _hydrate_candidates,
    _open_chunks_db_ro,
    _search_conn,
    execute_prepared_search,
    prepare_search,
    rerank_candidates,
    search,
    search_expanded,
    search_reranked,
)
from palace.watch.config import WatchRootsConfig, default_config_path
from tests.index.conftest import FakeEmbedder

_NOTES = {
    "alpha.md": "## Alpha\nlantern harbor at dusk\n",
    "beta.md": "## Beta\nlantern meadow notes\n",
    "gamma.md": "## Gamma\ncopper lantern kettle\n",
    "delta.md": "## Delta\nquiet garden path\n",
}


class RecordingExpander:
    """A deterministic expander whose rewrites repeat the query, as models may."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def hypothetical_passage(self, query: str) -> str:
        self.calls.append(("passage", query))
        return f"lantern passage for {query}"

    def rewrites(self, query: str, n: int) -> list[str]:
        self.calls.append(("rewrites", query, n))
        return [query, f"{query} meadow"][:n]

    def probe(self) -> None:
        self.calls.append(("probe",))

    def close(self) -> None:
        self.calls.append(("close",))


class FixtureReranker:
    """Scores by text length; optionally fails as an unavailable provider does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []
        self.closed = 0

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        if self.fail:
            raise RerankUnavailableError("fixture reranker unavailable")
        return [float(len(text) % 7) for text in texts]

    def probe(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("PALACE_STORE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    yield tmp_path


def _built(tmp_path: Path, name: str, notes: dict[str, str] = _NOTES) -> tuple[Path, Path]:
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    for file_name, body in notes.items():
        (root / file_name).write_text(body, encoding="utf-8")
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder())
    return store, root


def _reference(
    store: Path,
    *,
    route: Literal["direct", "fused", "reranked"],
    embedder: FakeEmbedder,
    expander: RecordingExpander | None,
    query: str,
    mode: Any,
    limit: int,
    pool: int,
    rerank_in: int,
    hyde: bool,
    multi_query: int | None,
    filters: Sequence[MetadataFilter],
) -> tuple[list[SearchHit], list[RerankCandidate]]:
    with closing(_open_chunks_db_ro(chunks_db_path(store))) as conn:
        conn.execute("BEGIN")
        if route == "direct":
            hits = _search_conn(
                conn,
                query=query,
                mode=mode,
                limit=limit,
                pool=pool,
                rrf_k=60,
                embedder=embedder,
                filters=filters,
            )
            return hits, []
        remote: dict[str, Any] = {}
        hits = _fuse_variants(
            conn,
            query=query,
            mode=mode,
            limit=rerank_in if route == "reranked" else limit,
            pool=pool,
            rrf_k=60,
            embedder=embedder,
            expander=expander,
            hyde=hyde,
            multi_query=multi_query,
            filters=filters,
            remote=remote,
        )
        if route != "reranked":
            return hits, []
        return hits, _hydrate_candidates(conn, [hit.chunk_id for hit in hits], remote)


def _expected_reranked(
    fused: list[SearchHit],
    candidates: list[RerankCandidate],
    *,
    query: str,
    limit: int,
    reranker: FixtureReranker,
    identity: RerankIdentity | None,
) -> RerankedSearch:
    try:
        ranked = rerank(query=query, candidates=candidates, top_k=limit, reranker=reranker)
    except RerankUnavailableError as exc:
        return RerankedSearch(fused[:limit], "failed", str(exc), identity)
    by_id = {hit.chunk_id: hit for hit in fused}
    hits = [
        replace(by_id[row.candidate.chunk_id], rerank_rank=row.rank, rerank_score=row.score)
        for row in ranked
    ]
    return RerankedSearch(hits, "applied", None, identity)


def _tagged(store: Path, root: Path) -> list[MetadataFilter]:
    with metadata_connection(chunks_db_path(store), writable=True) as conn:
        replace_documents(
            conn,
            [
                DocumentMetadata("doc-alpha", {"form": ["kept"]}, str(root), "alpha.md"),
                DocumentMetadata("doc-gamma", {"form": ["kept"]}, str(root), "gamma.md"),
            ],
        )
    return [MetadataFilter("form", any_of=["kept"])]


@pytest.mark.parametrize("composition", ["retrieval", "reranked"])
def test_single_store_entry_points_equal_prepare_then_execute(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, composition: str
) -> None:
    store, root = _built(isolated, "store")
    filters = _tagged(store, root)
    absent = isolated / "absent"
    absent.mkdir()

    if composition == "retrieval":
        cases: list[dict[str, Any]] = [
            {"route": "direct", "mode": "hybrid", "limit": 3, "pool": 10, "filters": []},
            {"route": "direct", "mode": "vector", "limit": 2, "pool": 4, "filters": filters},
            {
                "route": "fused",
                "mode": "hybrid",
                "limit": 3,
                "pool": 10,
                "hyde": True,
                "multi_query": 2,
                "filters": filters,
            },
            {"route": "fused", "mode": "bm25", "limit": 4, "pool": 10, "multi_query": 2},
        ]
        for case in cases:
            hyde, multi_query = case.get("hyde", False), case.get("multi_query")
            expanded = hyde or multi_query is not None
            arguments = {
                "query": "lantern harbor",
                "mode": case["mode"],
                "limit": case["limit"],
                "pool": case["pool"],
                "filters": case.get("filters", []),
            }
            live, live_expander = FakeEmbedder(), RecordingExpander() if expanded else None
            expected, _ = _reference(
                store,
                route=case["route"],
                embedder=live,
                expander=live_expander,
                rerank_in=0,
                hyde=hyde,
                multi_query=multi_query,
                **arguments,
            )
            direct, direct_expander = FakeEmbedder(), RecordingExpander()
            if case["route"] == "direct":
                entry = search(store=store, embedder=direct, **arguments)
            else:
                entry = search_expanded(
                    store=store,
                    embedder=direct,
                    expander=direct_expander,
                    hyde=hyde,
                    multi_query=multi_query,
                    **arguments,
                )
            seam, seam_expander = FakeEmbedder(), RecordingExpander()
            prepared = prepare_search(
                store=store,
                rerank_enabled=False,
                embedder=seam,
                expander=seam_expander,
                hyde=hyde,
                multi_query=multi_query,
                **arguments,
            )
            assert prepared.route == case["route"]
            executed = execute_prepared_search(prepared)
            assert executed == RerankedSearch(expected, "disabled", None)
            assert entry == expected and expected
            for embedder in (direct, seam):
                assert embedder.all_batches == live.all_batches
            if live_expander is not None:
                assert direct_expander.calls == seam_expander.calls == live_expander.calls

        # An absent database returns today's empty result, constructing no provider, and
        # refuses metadata filters exactly as before.
        idle, idle_expander = FakeEmbedder(), RecordingExpander()
        assert search(query="lantern", store=absent, embedder=idle) == []
        assert (
            search_expanded(
                query="lantern", store=absent, embedder=idle, expander=idle_expander, hyde=True
            )
            == []
        )
        assert execute_prepared_search(
            prepare_search(query="lantern", store=absent, rerank_enabled=False, embedder=idle)
        ) == RerankedSearch([], "disabled", None)
        for call in (search, search_expanded):
            with pytest.raises(SearchError, match="metadata unavailable: no chunks database"):
                call(query="lantern", store=absent, embedder=idle, filters=filters)
        assert idle.call_count == 0 and idle_expander.calls == []
        return

    # The reranked composition: the configured local reranker (whose identity the result
    # carries), an injected one, and the lenient failure posture.
    owned = FixtureReranker()
    monkeypatch.setattr(search_module, "load_reranker", lambda: owned)
    local_identity = RerankIdentity("local", RERANK_MODEL)
    for arguments in (
        {"mode": "hybrid", "limit": 2, "rerank_in": 4, "pool": 10, "filters": []},
        {
            "mode": "hybrid",
            "limit": 2,
            "rerank_in": 3,
            "pool": 10,
            "hyde": True,
            "multi_query": 2,
            "filters": filters,
        },
    ):
        live, live_expander = FakeEmbedder(), RecordingExpander()
        fused, candidates = _reference(
            store,
            route="reranked",
            embedder=live,
            expander=live_expander,
            query="lantern harbor",
            mode=arguments["mode"],
            limit=arguments["limit"],
            pool=arguments["pool"],
            rerank_in=arguments["rerank_in"],
            hyde=arguments.get("hyde", False),
            multi_query=arguments.get("multi_query"),
            filters=arguments["filters"],
        )
        assert fused and candidates
        retrieval = {k: v for k, v in arguments.items() if k != "limit"}
        pair_embedder, pair_expander = FakeEmbedder(), RecordingExpander()
        assert rerank_candidates(
            query="lantern harbor",
            store=store,
            embedder=pair_embedder,
            expander=pair_expander,
            **retrieval,
        ) == (fused, candidates)
        reference_reranker = FixtureReranker()
        expected = _expected_reranked(
            fused,
            candidates,
            query="lantern harbor",
            limit=arguments["limit"],
            reranker=reference_reranker,
            identity=local_identity,
        )
        entry_embedder, entry_expander = FakeEmbedder(), RecordingExpander()
        entry = search_reranked(
            query="lantern harbor",
            store=store,
            embedder=entry_embedder,
            expander=entry_expander,
            **arguments,
        )
        seam_embedder, seam_expander = FakeEmbedder(), RecordingExpander()
        seam = execute_prepared_search(
            prepare_search(
                query="lantern harbor",
                store=store,
                embedder=seam_embedder,
                expander=seam_expander,
                **arguments,
            )
        )
        assert entry == seam == expected and expected.status == "applied"
        for embedder in (pair_embedder, entry_embedder, seam_embedder):
            assert embedder.all_batches == live.all_batches
        for expander in (pair_expander, entry_expander, seam_expander):
            assert expander.calls == live_expander.calls
        # Each owned reranker scored the same texts and was closed by its call.
        assert owned.calls[-2:] == [reference_reranker.calls[0]] * 2

        # An injected reranker stays caller-owned; failure degrades only when lenient.
        for fail, strict in ((False, True), (True, False), (True, None)):
            injected = FixtureReranker(fail=fail)
            expected = _expected_reranked(
                fused,
                candidates,
                query="lantern harbor",
                limit=arguments["limit"],
                reranker=FixtureReranker(fail=fail),
                identity=None,
            )
            assert (
                search_reranked(
                    query="lantern harbor",
                    store=store,
                    embedder=FakeEmbedder(),
                    expander=RecordingExpander(),
                    reranker=injected,
                    strict=strict,
                    **arguments,
                )
                == expected
            )
            assert injected.closed == 0
        with pytest.raises(RerankUnavailableError, match="fixture reranker unavailable"):
            search_reranked(
                query="lantern harbor",
                store=store,
                embedder=FakeEmbedder(),
                expander=RecordingExpander(),
                reranker=FixtureReranker(fail=True),
                **arguments,
            )
    assert owned.closed == len(owned.calls)

    # An absent database loads the reranker configuration first, then returns today's
    # empty applied result without constructing an embedder, expander or reranker.
    owned_calls, idle, idle_expander = len(owned.calls), FakeEmbedder(), RecordingExpander()
    assert search_reranked(
        query="lantern", store=absent, embedder=idle, expander=idle_expander, multi_query=2
    ) == RerankedSearch([], "applied", None, local_identity)
    assert rerank_candidates(query="lantern", store=absent, embedder=idle) == ([], [])
    (absent / "meta").mkdir()
    (absent / "meta" / "reranker.toml").write_text("provider = 'unknown'\n")
    with pytest.raises(SearchError, match="reranker requires local defaults"):
        search_reranked(query="lantern", store=absent, embedder=idle)
    with pytest.raises(SearchError, match="metadata unavailable: no chunks database"):
        search_reranked(query="lantern", store=absent, reranker=owned, filters=filters)
    assert idle.call_count == 0 and idle_expander.calls == []
    assert len(owned.calls) == owned_calls


def test_prepared_single_store_refuses_changed_bindings_and_altered_values(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _built(isolated, "store")
    embedder = FakeEmbedder()

    def prepared(**overrides: Any) -> PreparedSearch:
        arguments: dict[str, Any] = {
            "query": "lantern",
            "store": store,
            "embedder": embedder,
            "injected_reranker": True,
        }
        return prepare_search(**{**arguments, **overrides})

    def no_retrieval(*_args: object, **_kwargs: object) -> list[SearchHit]:
        pytest.fail("a refused execution reached retrieval")

    reranker = FixtureReranker()
    value = prepared()
    with monkeypatch.context() as patch:
        patch.setattr(search_module, "_search_conn", no_retrieval)
        patch.setattr(search_module, "_fuse_variants", no_retrieval)
        calls = embedder.call_count

        # Only an unaltered value prepared in this process executes.
        for altered in (
            replace(value, limit=value.limit + 1),
            replace(value, seal="0" * 64),
            object(),
        ):
            with pytest.raises(SearchError, match="altered or not prepared in this process"):
                execute_prepared_search(altered, reranker=reranker)  # type: ignore[arg-type]
        with pytest.raises(SearchError, match="reranker injection differs from execution"):
            execute_prepared_search(value)
        with pytest.raises(SearchError, match="reranker injection differs from execution"):
            execute_prepared_search(prepared(injected_reranker=False), reranker=reranker)

        # A database that vanished or appeared since preparation refuses.
        database = chunks_db_path(store)
        database.rename(database.with_name("moved.sqlite"))
        with pytest.raises(SearchError, match="lost its chunks database.*prepare the query again"):
            execute_prepared_search(value, reranker=reranker)
        empty = prepared()
        assert empty.absent
        database.with_name("moved.sqlite").rename(database)
        with pytest.raises(SearchError, match="gained a chunks database.*prepare the query again"):
            execute_prepared_search(empty, reranker=reranker)

        # The configured reranker changed between the two steps.
        configured = prepared(injected_reranker=False)
        patch.setattr(
            search_module, "load_rerank_config", lambda _store: RerankConfig(model="other-model")
        )
        with pytest.raises(SearchError, match=r"binding changed for store .* \(reranker\)"):
            execute_prepared_search(configured)
        patch.undo()

    with monkeypatch.context() as patch:
        patch.setattr(search_module, "_search_conn", no_retrieval)
        patch.setattr(search_module, "_fuse_variants", no_retrieval)
        # A recorded identity that changed with its selector (both checks still pass).
        unchanged = prepared()
        save_embedder_config(
            store,
            EmbedderConfig(
                provider="openrouter",
                model="fixture-model",
                upstream="fixture-upstream",
                dim=EMBED_DIM,
                published_corpus=True,
                published_corpus_note="Synthetic published fixture",
                published_corpus_asserted_at="2026-09-06T00:00:00Z",
            ),
        )
        with closing(sqlite3.connect(chunks_db_path(store))) as conn:
            stamp_identity(conn, resolve_identity(store))
            conn.commit()
        with pytest.raises(SearchError, match=r"binding changed for store .* \(identity\)"):
            execute_prepared_search(unchanged, reranker=reranker)

        # Any selector edit, even an identity-neutral boundary note.
        neutral = prepared()
        save_embedder_config(
            store,
            replace(load_embedder_config(store), published_corpus_note="Edited synthetic note"),
        )
        with pytest.raises(SearchError, match=r"binding changed for store .* \(embedder\)"):
            execute_prepared_search(neutral, reranker=reranker)
    assert embedder.call_count == calls + 4
    assert reranker.calls == []


def test_prepared_single_store_execution_never_embeds_and_searches_current_rows(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _built(isolated, "store")
    embedder, expander = FakeEmbedder(), RecordingExpander()
    prepared = prepare_search(
        query="lantern",
        store=store,
        mode="hybrid",
        pool=10,
        limit=10,
        rerank_enabled=False,
        hyde=True,
        multi_query=2,
        embedder=embedder,
        expander=expander,
    )
    embedded, expanded = embedder.call_count, list(expander.calls)
    assert embedded == 3 and [call[0] for call in expanded] == ["rewrites", *["passage"] * 3]

    # Rows change between the two steps; execution searches the store as it is now.
    (root / "epsilon.md").write_text("## Epsilon\nlantern lighthouse keeper\n", encoding="utf-8")
    (root / "beta.md").unlink()
    build(store=store, watch_root_filter=root, embedder=FakeEmbedder())
    names = {Path(hit.path).name for hit in execute_prepared_search(prepared).hits}
    assert "epsilon.md" in names and "beta.md" not in names
    assert embedder.call_count == embedded and expander.calls == expanded

    # A retrieval asking for any unprepared provider input refuses instead.
    original = search_module._fuse_variants

    def asking(late: Any) -> Any:
        def wrapped(conn: sqlite3.Connection, **kwargs: Any) -> list[SearchHit]:
            late(kwargs)
            return original(conn, **kwargs)

        return wrapped

    for late, refusal in (
        (lambda kwargs: kwargs["embedder"].embed(["unprepared"]), "execution never embeds"),
        (
            lambda kwargs: kwargs["expander"].hypothetical_passage("unprepared"),
            "execution never expands",
        ),
        (lambda kwargs: kwargs["expander"].rewrites("unprepared", 2), "execution never expands"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(search_module, "_fuse_variants", asking(late))
            with pytest.raises(SearchError, match=refusal):
                execute_prepared_search(prepared)
    assert embedder.call_count == embedded and expander.calls == expanded

    # The value binds the store it was prepared for; another path needs a new value.
    moved = isolated / "moved"
    shutil.copytree(store, moved)
    with pytest.raises(SearchError, match="altered or not prepared in this process"):
        execute_prepared_search(replace(prepared, store=moved))
