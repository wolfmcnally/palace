"""Tests for :mod:`palace.search` (hybrid BM25 + vector + RRF search).

The engine is tested against a seeded chunks DB built via the real
:class:`palace.index.writer.WriterWorker` so the FTS5 and sqlite-vec
tables hold production-shape rows. ``FakeEmbedder`` (from
``tests.index.conftest``) provides deterministic content-hashed
vectors: embedding the literal string ``X`` yields the same vector
the writer stored for a chunk whose body is ``X``, so vector search
for ``X`` returns that chunk as the top hit.

The CLI is exercised via ``palace.cli.main`` argv-injection rather
than a subprocess, mirroring the rest of the suite.
"""

from __future__ import annotations

import json
import queue
import shutil
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from palace.cli import main as palace_main
from palace.index.embedder_config import resolve_identity
from palace.index.enrich import scored_text
from palace.index.writer import WriterWorker, _Sentinel, _WorkItem
from palace.search import (
    DEFAULT_LIMIT,
    RRF_K,
    SearchHit,
    search,
)
from tests.index.conftest import FakeEmbedder

# Make pytest's rewriting available to the imported fixtures' assertions too.
pytest.register_assert_rewrite("tests.index.conftest")


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir()
    return store


@pytest.fixture
def tmp_watch_root(tmp_path: Path) -> Path:
    root = tmp_path / "watch"
    root.mkdir()
    return root


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("PALACE_STORE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    yield


# --------------------------------------------------------------------- helpers


def _seed(*, store: Path, watch_root: Path, embedder: FakeEmbedder, files: dict[str, str]) -> None:
    """Drive the real WriterWorker to write ``files`` into the chunks DB."""
    completions: list[_WorkItem] = []
    work_queue: queue.Queue[_WorkItem | _Sentinel] = queue.Queue()
    worker = WriterWorker(
        store=store,
        embedder=embedder,
        identity=resolve_identity(store),
        work_queue=work_queue,
        on_completed=completions.append,
    )
    worker.start()
    try:
        for rel, body in files.items():
            path = watch_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            work_queue.put(
                _WorkItem(
                    change_kind="created",
                    path=path,
                    watch_root=watch_root,
                    file_kind="markdown",
                )
            )
        _wait_for(completions, len(files))
    finally:
        worker.stop()


def _wait_for(completions: list[_WorkItem], n: int) -> None:
    import time

    deadline = time.monotonic() + 2.0
    while len(completions) < n and time.monotonic() < deadline:
        time.sleep(0.01)


# --------------------------------------------------------------------- engine tests


def _assert_metadata_contract() -> None:
    from palace.metadata import (
        DocumentMetadata as D,
    )
    from palace.metadata import (
        MetadataError,
        lookup_documents,
        matching_documents,
        metadata_generation,
        replace_documents,
    )
    from palace.metadata import (
        MetadataFilter as F,
    )

    with closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
        docs = [
            D("a", {"form": ["order", "order", "letter"], "role": ["evidence"]}),
            D("b", {"form": ["order"], "role": ["argument"]}),
            D("c", {"form": ["letter"], "role": ["evidence"]}),
            D("d", {}),
        ]
        generation = replace_documents(conn, docs)
        assert replace_documents(conn, docs, delete_ids=["unknown"]) == generation

        def ids(filters):
            return [d.document_id for d in lookup_documents(conn, filters).documents]

        assert ids([]) == ["a", "b", "c", "d"]
        assert ids([F("form", any_of=["order"])]) == ["a", "b"]
        assert ids([F("form", any_of=["ORDER"])]) == []
        assert ids([F("form", any_of=["absent"])]) == []
        assert ids([F("form", any_of=["order", "letter"])]) == ["a", "b", "c"]
        assert ids([F("form", all_of=["order", "letter"])]) == ["a"]
        assert ids([F("form", any_of=["order", "letter"]), F("role", all_of=["evidence"])]) == [
            "a",
            "c",
        ]
        assert ids(
            [F("form", all_of=["order", "letter"]), F("role", any_of=["argument", "evidence"])]
        ) == ["a"]
        page = lookup_documents(conn, limit=2, count=True)
        assert page.total == 4 and page.cursor
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as other:
            replace_documents(other, docs)
            with pytest.raises(MetadataError, match="stale"):
                lookup_documents(other, cursor=page.cursor)
        assert [
            d.document_id for d in lookup_documents(conn, limit=2, cursor=page.cursor).documents
        ] == ["c", "d"]
        for predicate in [
            F("form"),
            F("form", any_of=["x"], all_of=["y"]),
            F("", any_of=["x"]),
            F("form", any_of="order"),
        ]:
            with pytest.raises(MetadataError):
                lookup_documents(conn, [predicate])
        for bad in [
            [D("a", {"form": ["changed"]}), D("b", {"form": [3]})],
            [D("a", {}), D("a", {})],
            [D("a", {}, "/root", "../bad")],
        ]:
            with pytest.raises(MetadataError):
                replace_documents(conn, bad)
            assert metadata_generation(conn) == generation
            assert ids([F("form", any_of=["order"])]) == ["a", "b"]
        with pytest.raises(MetadataError):
            replace_documents(conn, [D("a", {})], delete_ids=["a"])
        with pytest.raises(MetadataError):
            lookup_documents(conn, [F("form", any_of=["order"])], cursor=page.cursor)
        conn.execute("BEGIN")
        with pytest.raises(MetadataError, match="idle autocommit"):
            replace_documents(conn, [])
        assert conn.in_transaction
        conn.execute("ROLLBACK")
        replace_documents(conn, [D("a", {"form": ["new"]})], delete_ids=["b"])
        assert ids([F("form", any_of=["order"])]) == []
        with pytest.raises(MetadataError, match="stale"):
            lookup_documents(conn, cursor=page.cursor)
        replace_documents(conn, [D("file-a", {}, "/root", "same.md")])
        before = metadata_generation(conn)
        with pytest.raises(MetadataError):
            replace_documents(conn, [D("z", {}), D("file-b", {}, "/root", "same.md")])
        assert metadata_generation(conn) == before and "z" not in ids([])
        replace_documents(conn, [D("file-b", {}, "/other", "same.md")])
        assert "file-b" in ids([])

    work = []
    broad = []
    for size in (1000, 100000):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
            replace_documents(conn, [])
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO metadata_documents VALUES (?,NULL,NULL)",
                ((f"doc-{i:06d}",) for i in range(size)),
            )
            conn.executemany(
                "INSERT INTO metadata_values VALUES ('tag',?,?)",
                (("rare" if i < 10 else "common", f"doc-{i:06d}") for i in range(size)),
            )
            conn.execute("COMMIT")
            filters = [F("tag", any_of=["rare"])]
            sql, args = matching_documents(filters)
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
            assert any("SEARCH metadata_values USING PRIMARY KEY" in row[3] for row in plan)
            for selected, measurements in [(filters, work), ([F("tag", any_of=["common"])], broad)]:
                steps = [0]

                def progress(steps=steps):
                    steps[0] += 1
                    return 0

                conn.set_progress_handler(progress, 1)
                page = lookup_documents(conn, selected, limit=5, count=True)
                conn.set_progress_handler(None, 0)
                assert len(page.documents) == 5
                measurements.append(steps[0])
    assert work[1] <= work[0] * 3, work
    assert broad[1] > broad[0] * 10, broad


def _assert_filtered_retrieval(store: Path, root: Path, embedder: FakeEmbedder) -> None:
    from palace.index.config import chunks_db_path
    from palace.metadata import (
        DocumentMetadata as D,
    )
    from palace.metadata import (
        MetadataFilter as F,
    )
    from palace.metadata import (
        lookup_documents,
        metadata_connection,
        replace_documents,
    )
    from palace.search import _open_chunks_db_ro, search_expanded

    filters = [F("form", any_of=["eligible"])]
    with closing(_open_chunks_db_ro(chunks_db_path(store))) as reader:
        before = (
            reader.execute("SELECT * FROM chunks ORDER BY chunk_id").fetchall(),
            reader.execute(
                "SELECT chunk_id,embedding FROM chunks_vec ORDER BY chunk_id"
            ).fetchall(),
        )
    calls = embedder.call_count
    with metadata_connection(chunks_db_path(store), writable=True) as conn:
        replace_documents(
            conn,
            [
                D("doc-beta", {"form": ["eligible"]}, str(root), "beta.md"),
                D("no-text", {"form": ["eligible"]}),
                D("other-root", {"form": ["other"]}, "/other", "beta.md"),
            ],
        )
        assert len(lookup_documents(conn, filters).documents) == 2
    assert embedder.call_count == calls
    with closing(_open_chunks_db_ro(chunks_db_path(store))) as reader:
        after = (
            reader.execute("SELECT * FROM chunks ORDER BY chunk_id").fetchall(),
            reader.execute(
                "SELECT chunk_id,embedding FROM chunks_vec ORDER BY chunk_id"
            ).fetchall(),
        )
    assert before == after
    query = scored_text(path="alpha.md", heading=None, body="alpha")
    for pool in (1, 10):
        hits = search(
            query=query,
            store=store,
            mode="vector",
            embedder=embedder,
            pool=pool,
            limit=1,
            filters=filters,
        )
        assert [Path(h.path).name for h in hits] == ["beta.md"]
    assert (
        search(
            query=query,
            store=store,
            mode="vector",
            embedder=embedder,
            pool=1,
            filters=[F("form", any_of=["absent"])],
        )
        == []
    )
    assert search(query="alpha", store=store, mode="bm25", filters=filters, pool=1) == []
    assert [
        Path(h.path).name
        for h in search_expanded(query="beta", store=store, mode="bm25", filters=filters, pool=1)
    ] == ["beta.md"]

    class Expansion:
        def hypothetical_passage(self, query):
            return "alpha"

        def rewrites(self, query, n):
            return ["alpha"] * n

        def probe(self):
            pass

        def close(self):
            pass

    class Ranking:
        def score(self, query, texts):
            assert texts and all("beta" in text for text in texts)
            return [1.0] * len(texts)

        def probe(self):
            pass

        def close(self):
            pass

    from palace.search import search_reranked

    expanded = search_reranked(
        query="beta",
        store=store,
        mode="hybrid",
        embedder=embedder,
        expander=Expansion(),
        reranker=Ranking(),
        hyde=True,
        multi_query=2,
        filters=filters,
        pool=1,
        rerank_in=1,
    )
    assert [Path(hit.path).name for hit in expanded.hits] == ["beta.md"]
    _seed(
        store=store,
        watch_root=root,
        embedder=embedder,
        files={"alpha.md": "needle", "beta.md": "needle " + "padding " * 100},
    )
    assert Path(search(query="needle", store=store, mode="bm25", pool=1)[0].path).name != "beta.md"
    eligible = search(query="needle", store=store, mode="bm25", pool=1, filters=filters)
    assert [Path(hit.path).name for hit in eligible] == ["beta.md"]
    from palace.index._errors import IndexError as SearchError

    with metadata_connection(chunks_db_path(store), writable=True) as conn:
        conn.execute("DROP TABLE metadata_state")
    before_calls = embedder.call_count
    with pytest.raises(SearchError, match="metadata unavailable"):
        search(query="alpha", store=store, filters=filters, embedder=embedder)
    assert embedder.call_count == before_calls


def test_search_returns_empty_when_db_missing(tmp_store: Path) -> None:
    """A fresh store with no chunks DB should resolve to an empty result list."""
    _assert_metadata_contract()
    from palace.index._errors import IndexError as SearchError
    from palace.metadata import MetadataFilter

    with pytest.raises(SearchError, match="metadata unavailable"):
        search(query="anything", store=tmp_store, filters=[MetadataFilter("tag", any_of=["x"])])
    hits = search(query="anything", store=tmp_store, mode="hybrid")
    assert hits == []
    assert list(tmp_store.iterdir()) == []
    from palace.index._errors import IndexError as SearchError
    from palace.multistore import search_stores

    absent = tmp_store / "absent"
    with pytest.raises(SearchError, match="no chunks database"):
        search_stores(query="anything", stores=[absent])
    assert not absent.exists()
    with pytest.raises(SearchError, match="duplicate store"):
        search_stores(query="anything", stores=[tmp_store, tmp_store / "."])

    corrupt = tmp_store / "index" / "chunks.sqlite"
    corrupt.parent.mkdir()
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(SearchError, match="database read failed"):
        search_stores(query="anything", stores=[tmp_store])
    assert corrupt.read_bytes() == b"not a sqlite database"


def test_search_invalid_mode_raises(tmp_store: Path) -> None:
    from palace.index._errors import IndexError as _IndexError

    with pytest.raises(_IndexError, match="search query is empty"):
        search(query="   ", store=tmp_store)
    with pytest.raises(_IndexError):
        search(query="x", store=tmp_store, mode="elastic")  # type: ignore[arg-type]
    with pytest.raises(_IndexError):
        search(query="x", store=tmp_store, limit=0)


def test_search_bm25_only_returns_lexical_matches(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={
            "a.md": "## A\nthe quick brown fox\n",
            "b.md": "## B\nlazy dog under the tree\n",
            "c.md": "## C\nfox hunting season\n",
        },
    )
    hits = search(query="fox", store=tmp_store, mode="bm25")
    assert {Path(h.path).name for h in hits} == {"a.md", "c.md"}
    for hit in hits:
        assert hit.bm25_rank is not None
        assert hit.vec_rank is None
        assert "<<" in hit.snippet  # FTS5 snippet markup
    # Pure BM25 score has no vec_term contribution.
    for h in hits:
        assert h.bm25_rank is not None
        assert h.rrf_score == pytest.approx(1.0 / (RRF_K + h.bm25_rank))


def test_search_vector_only_returns_semantic_neighbors(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    """FakeEmbedder is content-hashed, so querying one chunk's exact enriched
    representation makes that chunk the top vector hit by construction.
    """
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={
            "alpha.md": "alpha\n",
            "beta.md": "beta\n",
            "gamma.md": "gamma\n",
        },
    )
    alpha_scored_text = scored_text(path="alpha.md", heading=None, body="alpha")
    hits = search(query=alpha_scored_text, store=tmp_store, mode="vector", embedder=embedder)
    assert hits
    # The exact scored-text chunk wins on cosine distance.
    assert Path(hits[0].path).name == "alpha.md"
    for hit in hits:
        assert hit.vec_rank is not None
        assert hit.bm25_rank is None

    _assert_filtered_retrieval(tmp_store, tmp_watch_root, embedder)


def test_query_embedding_is_not_breadcrumbed(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    _assert_supplied_search(tmp_store / "supplied", tmp_watch_root, embedder)
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"alpha.md": "alpha\n"},
    )
    query = "literal query bytes"
    search(query=query, store=tmp_store, mode="vector", embedder=embedder)
    assert embedder.last_batch == [query]

    from dataclasses import fields, replace

    import palace.multistore as multiple
    from palace.index._errors import IndexError as SearchError
    from palace.index.config import EMBED_DIM, chunks_db_path
    from palace.index.embedder_config import (
        EmbedderConfig,
        load_embedder_config,
        resolve_identity,
        save_embedder_config,
    )
    from palace.index.schema import stamp_identity
    from palace.index.store_ops import remove_paths
    from palace.multistore import (
        PreparedStoreSearch,
        execute_prepared,
        prepare_store_search,
        search_stores,
    )

    copied = tmp_store.parent / "copied-store"
    shutil.copytree(tmp_store, copied)
    identity = resolve_identity(tmp_store)
    before = embedder.call_count
    combined = search_stores(
        query=query,
        stores=[tmp_store, copied],
        rerank_enabled=False,
        embedders={identity: embedder},
    )
    assert embedder.call_count == before + 1
    assert len(combined.hits) == 2
    assert len({item.origin_id for item in combined.hits}) == 2
    assert len({item.hit.chunk_id for item in combined.hits}) == 1

    class Expansion:
        def __init__(self) -> None:
            self.rewrite_calls = 0
            self.passages: list[str] = []

        def rewrites(self, text: str, n: int) -> list[str]:
            self.rewrite_calls += 1
            return [f"{text} alternative {i}" for i in range(n)]

        def hypothetical_passage(self, text: str) -> str:
            self.passages.append(text)
            return f"hypothetical {text}"

        def probe(self) -> None:
            pass

        def close(self) -> None:
            raise AssertionError("injected expander remains caller-owned")

    expansion = Expansion()
    before = embedder.call_count
    expanded = search_stores(
        query=query,
        stores=[tmp_store, copied],
        rerank_enabled=False,
        embedders={identity: embedder},
        expander=expansion,
        hyde=True,
        multi_query=2,
    )
    assert len(expanded.hits) == 2
    assert expansion.rewrite_calls == 1
    assert len(expansion.passages) == len(set(expansion.passages)) == 3
    assert embedder.call_count == before + 3

    # Preparation makes every expansion and embedding call; execution makes none and
    # returns the single call's result.
    staged = Expansion()
    before = embedder.call_count
    prepared = prepare_store_search(
        query=query,
        stores=[tmp_store, copied],
        rerank_enabled=False,
        embedders={identity: embedder},
        expander=staged,
        hyde=True,
        multi_query=2,
    )
    assert (staged.rewrite_calls, len(staged.passages), embedder.call_count - before) == (1, 3, 3)
    assert execute_prepared(prepared) == expanded
    assert (staged.rewrite_calls, len(staged.passages), embedder.call_count - before) == (1, 3, 3)

    # A query whose BM25 leg refuses is never embedded, prepared or not.
    before = embedder.call_count
    with pytest.raises(SearchError, match="no searchable term"):
        prepare_store_search(
            query="???", stores=[tmp_store, copied], embedders={identity: embedder}
        )
    with pytest.raises(SearchError, match="no searchable term"):
        search_stores(query="???", stores=[tmp_store, copied], embedders={identity: embedder})
    assert embedder.call_count == before

    # A pool beyond sqlite-vec's KNN limit refuses with the extension's own text before
    # any embedding; BM25 has no such limit. The bound matches the locked extension.
    import re

    import sqlite_vec

    from palace.search import SQLITE_VEC_MAX_K

    too_large = SQLITE_VEC_MAX_K + 1
    expected = (
        f"k value in knn query too large, provided {too_large} and the limit is {SQLITE_VEC_MAX_K}"
    )
    bounded = f"multi-store database read failed: {re.escape(expected)}"
    stores = [tmp_store, copied]
    with pytest.raises(SearchError, match=bounded):
        prepare_store_search(
            query=query, stores=stores, pool=too_large, embedders={identity: embedder}
        )
    with pytest.raises(SearchError, match=bounded):
        search_stores(query=query, stores=stores, pool=too_large, embedders={identity: embedder})
    lexical = prepare_store_search(
        query=query, stores=stores, mode="bm25", pool=too_large, rerank_enabled=False
    )
    assert execute_prepared(lexical).status == "disabled"
    assert embedder.call_count == before
    with closing(sqlite3.connect(chunks_db_path(tmp_store))) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        probe = sqlite_vec.serialize_float32([0.0] * EMBED_DIM)
        with pytest.raises(sqlite3.OperationalError) as raised:
            conn.execute(
                "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ?",
                (probe, too_large),
            ).fetchall()
    assert str(raised.value) == expected

    # Execution searches current rows: a file indexed or removed after preparation is
    # found or gone, without embedding again.
    plain = prepare_store_search(
        query=query,
        stores=[tmp_store, copied],
        rerank_enabled=False,
        embedders={identity: embedder},
    )
    _seed(store=tmp_store, watch_root=tmp_watch_root, embedder=embedder, files={"added.md": query})
    before = embedder.call_count

    def names(result: multiple.MultiStoreSearch) -> set[str]:
        return {Path(item.hit.path).name for item in result.hits}

    assert "added.md" in names(execute_prepared(plain))
    remove_paths(store=tmp_store, watch_root=tmp_watch_root, paths=["added.md"])
    assert "added.md" not in names(execute_prepared(plain))
    # Nor does it bind the watch-root set: a root indexed after preparation is searched.
    second_root = tmp_watch_root.parent / "second-watch"
    _seed(store=tmp_store, watch_root=second_root, embedder=embedder, files={"second.md": query})
    before = embedder.call_count
    assert "second.md" in names(execute_prepared(plain))
    remove_paths(store=tmp_store, watch_root=second_root, paths=["second.md"])
    assert embedder.call_count == before

    # Preparation holds nothing afterwards: constructed providers and every snapshot it
    # opened close on success and on refusal; injected providers are never closed.
    class Owned(FakeEmbedder):
        closed = 0

        def close(self) -> None:
            self.closed += 1

    class OwnedExpansion(Expansion):
        closed = 0

        def close(self) -> None:
            self.closed += 1

    opened: list[sqlite3.Connection] = []
    open_snapshot = multiple._open_chunks_db_ro

    def tracked(db: Path) -> sqlite3.Connection:
        opened.append(open_snapshot(db))
        return opened[-1]

    owned, owned_expansion = Owned(), OwnedExpansion()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            multiple, "resolve_embedder", lambda root, *, purpose: (owned, resolve_identity(root))
        )
        patch.setattr(multiple, "OllamaQueryExpander", lambda: owned_expansion)
        patch.setattr(multiple, "_open_chunks_db_ro", tracked)
        stores = [tmp_store, copied]
        prepare_store_search(query=query, stores=stores, rerank_enabled=False, multi_query=2)
        assert (owned.closed, owned_expansion.closed) == (1, 1)
        owned.raise_on_call = SearchError("fixture embedding failure")
        with pytest.raises(SearchError, match="fixture embedding failure"):
            prepare_store_search(query=query, stores=stores, rerank_enabled=False, multi_query=2)
        assert (owned.closed, owned_expansion.closed) == (2, 2)
        injected = Owned()
        prepare_store_search(
            query=query,
            stores=stores,
            rerank_enabled=False,
            embedders={identity: injected},
            expander=Expansion(),
            multi_query=2,
        )
        assert injected.closed == 0
    assert len(opened) == 6
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    # An altered, hand-made or foreign value refuses before any store is admitted.
    values = {item.name: getattr(plain, item.name) for item in fields(plain)}
    with pytest.MonkeyPatch.context() as patch:

        def unadmitted(*_args: object, **_kwargs: object) -> None:
            pytest.fail("an unsealed prepared value reached store admission")

        patch.setattr(multiple, "_admit_stores", unadmitted)
        for altered in (
            replace(plain, query="other"),
            replace(plain, limit=plain.limit + 1),
            PreparedStoreSearch(**{**values, "seal": "0" * 64}),
            object(),
        ):
            with pytest.raises(SearchError, match="altered or not prepared in this process"):
                execute_prepared(altered)  # type: ignore[arg-type]

    # Execution never embeds or expands: an unprepared input refuses.
    original = multiple._fuse_variants

    def calling(late: Callable[[dict[str, Any]], object]) -> Callable[..., list[SearchHit]]:
        def wrapped(conn: sqlite3.Connection, **kwargs: Any) -> list[SearchHit]:
            late(kwargs)
            return original(conn, **kwargs)

        return wrapped

    before = embedder.call_count
    for late, refusal in (
        (lambda kwargs: kwargs["embedder"].embed(["unprepared"]), "execution never embeds"),
        (
            lambda kwargs: kwargs["expander"].hypothetical_passage("unprepared"),
            "execution never expands",
        ),
        (lambda kwargs: kwargs["expander"].rewrites("unprepared", 2), "execution never expands"),
    ):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(multiple, "_fuse_variants", calling(late))
            with pytest.raises(SearchError, match=refusal):
                execute_prepared(prepared)
    assert embedder.call_count == before
    assert (staged.rewrite_calls, len(staged.passages)) == (1, 3)

    unchanged = prepare_store_search(
        query=query,
        stores=[copied, tmp_store],
        rerank_enabled=False,
        embedders={identity: embedder},
    )

    save_embedder_config(
        copied,
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
    different = resolve_identity(copied)
    with closing(sqlite3.connect(chunks_db_path(copied))) as conn:
        stamp_identity(conn, different)
        conn.commit()
    with pytest.raises(SearchError, match=r"binding changed for store .*copied-store \(identity\)"):
        execute_prepared(unchanged)
    second = FakeEmbedder()
    before = embedder.call_count
    separated = search_stores(
        query=query,
        stores=[copied, tmp_store],
        rerank_enabled=False,
        embedders={identity: embedder, different: second},
    )
    assert len(separated.hits) == 2
    assert embedder.call_count == before + 1
    assert second.call_count == 1
    assert embedder.last_batch == second.last_batch == [query]

    # Any selector edit, even an identity-neutral boundary note, requires preparing again.
    neutral = prepare_store_search(
        query=query,
        stores=[copied, tmp_store],
        rerank_enabled=False,
        embedders={identity: embedder, different: FakeEmbedder()},
    )
    save_embedder_config(
        copied, replace(load_embedder_config(copied), published_corpus_note="Edited synthetic note")
    )
    with pytest.raises(SearchError, match=r"copied-store \(embedder\)"):
        execute_prepared(neutral)

    # Security admission precedes the vector-leg bound: a store outside the published
    # boundary refuses with the boundary text, and nothing is embedded.
    asserted = load_embedder_config(copied)
    save_embedder_config(copied, replace(asserted, published_corpus=False))
    with pytest.raises(SearchError, match="complete published-corpus assertion"):
        prepare_store_search(
            query=query,
            stores=[copied, tmp_store],
            pool=too_large,
            embedders={identity: embedder, different: second},
        )
    save_embedder_config(copied, asserted)

    with pytest.raises(SearchError, match="no injected query embedder"):
        search_stores(query=query, stores=[copied, tmp_store], embedders={identity: embedder})
    assert second.call_count == 1
    with closing(sqlite3.connect(chunks_db_path(copied))) as conn:
        conn.execute("UPDATE index_meta SET value='wrong' WHERE key='embed_model'")
        conn.commit()
    before = embedder.call_count
    with pytest.raises(SearchError, match="embedding-identity mismatch"):
        search_stores(
            query=query,
            stores=[tmp_store, copied],
            embedders={identity: embedder, different: second},
        )
    assert embedder.call_count == before
    assert second.call_count == 1


def test_search_hybrid_fuses_via_rrf(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    """A chunk that appears in BOTH retrievers must outrank a chunk that
    appears in only one (assuming similar ranks).
    """
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={
            # 'wolverine' in BM25 AND embedding is closest to a 'wolverine' query.
            "match.md": "wolverine\n",
            # Contains 'wolverine' so BM25 hits, but different body so vec misses.
            "lexical.md": "wolverine and other animals here\n",
            # Unrelated body — vector-only pool entry.
            "semantic.md": "alpha\n",
        },
    )
    hits = search(query="wolverine", store=tmp_store, mode="hybrid", embedder=embedder)
    # Both BM25 hits should appear; the one that's also vec-close to "wolverine"
    # (i.e. match.md's own body) outranks the one that isn't.
    assert hits
    top = hits[0]
    assert Path(top.path).name == "match.md"
    assert top.bm25_rank is not None
    assert top.vec_rank is not None
    # And the lexical-only hit is still present, scored lower.
    paths = [Path(h.path).name for h in hits]
    assert "lexical.md" in paths
    # Consensus rrf > single-retriever rrf, all else equal.
    match_hit = next(h for h in hits if Path(h.path).name == "match.md")
    lexical_hit = next(h for h in hits if Path(h.path).name == "lexical.md")
    assert match_hit.rrf_score > lexical_hit.rrf_score

    # Exercise the synchronous producer and its incremental/portable contract
    # with the same deterministic retrieval oracle on every platform.
    from palace.index.build import build
    from palace.rerank_bench import BenchQuestion, _run_questions, render_report

    sync_store = tmp_store.parent / "sync-store"
    assert build(
        store=sync_store, watch_root_filter=tmp_watch_root, full=True, embedder=embedder
    ).roots
    first_calls = embedder.call_count
    assert build(store=sync_store, watch_root_filter=tmp_watch_root, embedder=embedder).roots
    assert embedder.call_count == first_calls
    (tmp_watch_root / "lexical.md").write_text("wolverine updated policy\n")
    (tmp_watch_root / "semantic.md").unlink()
    assert build(store=sync_store, watch_root_filter=tmp_watch_root, embedder=embedder).roots
    assert embedder.call_count == first_calls + 1
    portable = tmp_store.parent / "portable-store"
    shutil.copytree(sync_store, portable)
    moved_hits = search(query="wolverine", store=portable, embedder=embedder)
    assert {Path(hit.path).name for hit in moved_hits} == {"match.md", "lexical.md"}
    assert (
        next(hit.chunk_id for hit in moved_hits if Path(hit.path).name == "match.md")
        == top.chunk_id
    )

    class FixtureReranker:
        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            return [float("match.md" in text) for text in texts]

        def probe(self) -> None:
            pass

        def close(self) -> None:
            pass

    measured = _run_questions(
        questions=[
            BenchQuestion(
                id="portable-fixture",
                question="wolverine",
                expected_facts=[],
                expected_sources=["match.md"],
                tags=["synthetic"],
                added="2026-09-06",
            )
        ],
        store=portable,
        rerank_in=30,
        limit=10,
        repeats=2,
        embedder=embedder,
        reranker_instance=FixtureReranker(),
    )
    assert measured[0].on_status == "applied"
    assert measured[0].on_expected_rank == 1
    assert len(measured[0].rerank_ms_samples) == 2
    assert all(sample >= 0 for sample in measured[0].rerank_ms_samples)
    report = render_report(
        results=measured,
        store=portable,
        model_dir=portable / "fixture-model",
        rerank_in=30,
        limit=10,
        repeats=2,
        model_load_ms=0,
    )
    assert "portable-fixture" in report and "status=applied" in report


def test_search_limit_respected(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {f"f{i:02}.md": f"common body {i}\n" for i in range(20)}
    _seed(store=tmp_store, watch_root=tmp_watch_root, embedder=embedder, files=files)
    hits = search(query="common", store=tmp_store, mode="bm25", limit=5)
    assert len(hits) == 5
    assert len(search(query="common", store=tmp_store, mode="bm25")) == DEFAULT_LIMIT == 10

    from dataclasses import replace

    import palace.multistore as multiple
    from palace.rerank import RerankUnavailableError

    copied = tmp_store.parent / "copied-store"
    shutil.copytree(tmp_store, copied)
    original = multiple._fuse_variants

    def incompatible_scores(conn: sqlite3.Connection, **kwargs: object) -> list[SearchHit]:
        rows = original(conn, **kwargs)
        location = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        score = 1_000_000 if copied in location.parents else -1_000_000
        return [replace(row, rrf_score=float(score)) for row in rows]

    monkeypatch.setattr(multiple, "_fuse_variants", incompatible_scores)
    first = multiple.search_stores(
        query="common", stores=[tmp_store, copied], mode="bm25", rerank_enabled=False, limit=5
    )
    reverse = multiple.search_stores(
        query="common", stores=[copied, tmp_store], mode="bm25", rerank_enabled=False, limit=5
    )
    assert first == reverse
    assert first.status == "disabled"
    assert len(first.hits) == len({hit.origin_id for hit in first.hits}) == 5
    assert first.hits[0].hit.rrf_score == first.hits[1].hit.rrf_score == 1 / (RRF_K + 1)
    assert {item.store for item in first.hits[:2]} == {tmp_store, copied}

    class Scorer:
        calls = 0
        fail = False

        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            self.calls += 1
            if self.fail:
                raise RerankUnavailableError("fixture failure")
            assert len(texts) == 40  # One union, with local identifier collisions preserved.
            return [0.0 for _ in texts]

        def probe(self) -> None:
            pass

        def close(self) -> None:
            pytest.fail("injected reranker is caller-owned")

    scorer = Scorer()
    ranked = multiple.search_stores(
        query="common", stores=[tmp_store, copied], mode="bm25", limit=5, reranker=scorer
    )
    assert scorer.calls == 1 and ranked.status == "applied"
    scorer.fail = True
    with pytest.raises(RerankUnavailableError, match="fixture failure"):
        multiple.search_stores(
            query="common", stores=[tmp_store, copied], mode="bm25", reranker=scorer
        )
    failed = multiple.search_stores(
        query="common",
        stores=[tmp_store, copied],
        mode="bm25",
        limit=5,
        reranker=scorer,
        strict=False,
    )
    assert failed.status == "failed" and failed.detail == "fixture failure"
    assert failed.hits == first.hits

    from palace.index._errors import IndexError as SearchError
    from palace.index.config import chunks_db_path
    from palace.multistore import PreparedStoreSearch, execute_prepared, prepare_store_search

    def prepared_pair(*, injected: bool) -> PreparedStoreSearch:
        return prepare_store_search(
            query="common",
            stores=[tmp_store, copied],
            mode="bm25",
            limit=5,
            injected_reranker=injected,
        )

    # The reranker declared at preparation is the one execution must receive.
    with pytest.raises(SearchError, match="reranker injection differs from execution"):
        execute_prepared(prepared_pair(injected=True))
    with pytest.raises(SearchError, match="reranker injection differs from execution"):
        execute_prepared(prepared_pair(injected=False), reranker=scorer)
    calls = scorer.calls
    with pytest.raises(RerankUnavailableError, match="fixture failure"):
        execute_prepared(prepared_pair(injected=True), reranker=scorer)
    assert scorer.calls == calls + 1

    # A store whose database vanished after preparation refuses before retrieval.
    prepared = prepared_pair(injected=True)
    database = chunks_db_path(copied)
    database.rename(database.with_name("moved.sqlite"))
    with pytest.raises(SearchError, match="store has no chunks database"):
        execute_prepared(prepared, reranker=scorer)
    assert scorer.calls == calls + 1


def test_search_uses_bm25_snippet_when_available(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"a.md": "## H\nphrase with searchable target inside\n"},
    )
    hits = search(query="searchable", store=tmp_store, mode="bm25")
    assert hits
    assert "<<searchable>>" in hits[0].snippet


def test_search_falls_back_to_body_preview_for_vector_only_hits(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    """A vector-only hit has no BM25 snippet — the preview is body-truncated."""
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"a.md": "alpha\n"},
    )
    # Query a string with no lexical overlap but the FakeEmbedder still produces
    # a deterministic vector; the only chunk in the DB will be the closest neighbor.
    hits = search(query="alpha", store=tmp_store, mode="vector", embedder=embedder)
    assert hits
    assert "<<" not in hits[0].snippet  # no FTS5 markup
    assert "alpha" in hits[0].snippet  # body preview


# --------------------------------------------------------------------- CLI tests


def test_palace_search_no_results_exits_zero(
    clean_env: None,
    tmp_store: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = palace_main(
        [
            "search",
            "nothing-matches-this-token-xyzzy",
            "--store",
            str(tmp_store),
            "--mode",
            "bm25",
            "--no-rerank",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "no results" in out


def test_palace_search_against_seeded_db_human_output(
    clean_env: None,
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"only.md": "## H\nuniquetokenfox content here\n"},
    )
    rc = palace_main(
        [
            "search",
            "uniquetokenfox",
            "--store",
            str(tmp_store),
            "--mode",
            "bm25",
            "--no-rerank",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "only.md" in out
    assert "<<uniquetokenfox>>" in out


def test_palace_search_json_emits_jsonl(
    clean_env: None,
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"only.md": "## H\nuniquetokenfox content here\n"},
    )
    rc = palace_main(
        [
            "search",
            "uniquetokenfox",
            "--store",
            str(tmp_store),
            "--mode",
            "bm25",
            "--no-rerank",
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines
    parsed = json.loads(lines[0])
    assert "chunk_id" in parsed
    assert "rrf_score" in parsed
    assert "bm25_rank" in parsed
    assert parsed["bm25_rank"] == 1
    assert parsed["rerank_rank"] is None
    assert parsed["rerank_score"] is None
    assert parsed["rerank_status"] == "disabled"

    copied = tmp_store.parent / "copied-store"
    shutil.copytree(tmp_store, copied)
    rc = palace_main(
        [
            "search",
            "uniquetokenfox",
            "--store",
            str(tmp_store),
            "--store",
            str(copied),
            "--mode",
            "bm25",
            "--no-rerank",
            "--json",
        ]
    )
    assert rc == 0
    combined = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(combined) == 2
    assert {row["store"] for row in combined} == {str(tmp_store), str(copied)}
    assert all(row["origin_id"] == [row["store"], row["chunk_id"]] for row in combined)
    assert {row["rerank_status"] for row in combined} == {"disabled"}

    from palace.rerank import RerankUnavailableError

    def unavailable() -> None:
        raise RerankUnavailableError("fixture reranker unavailable")

    monkeypatch.setattr("palace.multistore.load_reranker", unavailable)
    monkeypatch.setattr("palace.rerank._RERANK_WARNING_EMITTED", False)
    argv = [
        "search",
        "uniquetokenfox",
        "--store",
        str(tmp_store),
        "--store",
        str(copied),
        "--mode",
        "bm25",
        "--json",
    ]
    assert palace_main(argv) == 1
    failed = capsys.readouterr()
    assert failed.out == ""
    assert "fixture reranker unavailable" in failed.err
    assert palace_main([*argv, "--allow-rerank-failure"]) == 0
    degraded = capsys.readouterr()
    assert "rerank" in degraded.err
    assert {json.loads(line)["rerank_status"] for line in degraded.out.splitlines()} == {"failed"}

    batch = tmp_store / "metadata.json"
    batch.write_text(
        json.dumps({"documents": [{"document_id": "doc-only", "fields": {"kind": ["report"]}}]})
    )
    assert (
        palace_main(["metadata", "replace", "--store", str(tmp_store), "--input", str(batch)]) == 0
    )
    capsys.readouterr()
    assert (
        palace_main(
            [
                "metadata",
                "lookup",
                "--store",
                str(tmp_store),
                "--count",
                "--filters",
                '[{"field":"kind","any_of":["report"]}]',
            ]
        )
        == 0
    )
    metadata_output = json.loads(capsys.readouterr().out)
    assert metadata_output["total"] == 1
    assert metadata_output["documents"][0]["document_id"] == "doc-only"
    assert (
        palace_main(
            [
                "search",
                "anything",
                "--store",
                str(tmp_store),
                "--store",
                str(tmp_store / "other"),
                "--metadata-filters",
                '[{"field":"kind","any_of":["report"]}]',
            ]
        )
        == 1
    )
    assert "single store" in capsys.readouterr().err

    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"excluded.md": "uniquetokenfox"},
    )
    batch.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": "associated",
                        "fields": {"kind": ["report"]},
                        "watch_root": str(tmp_watch_root),
                        "path": "only.md",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("PALACE_STORE", str(tmp_store))
    assert palace_main(["metadata", "replace", "--input", str(batch)]) == 0
    capsys.readouterr()
    assert (
        palace_main(
            [
                "search",
                "uniquetokenfox",
                "--mode",
                "bm25",
                "--no-rerank",
                "--json",
                "--metadata-filters",
                '[{"field":"kind","any_of":["report"]}]',
            ]
        )
        == 0
    )
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [Path(row["path"]).name for row in output] == ["only.md"]
    from palace.metadata import (
        DocumentMetadata,
        MetadataError,
        initialize_metadata,
        replace_documents,
    )

    database = tmp_store / "index/chunks.sqlite"
    with (
        closing(sqlite3.connect(database, isolation_level=None)) as writer,
        closing(sqlite3.connect(database, isolation_level=None, timeout=0)) as reader,
    ):
        writer.execute("BEGIN IMMEDIATE")
        initialize_metadata(reader)  # Already initialized: no write lock is needed.
        with pytest.raises(MetadataError, match="locked"):
            replace_documents(reader, [DocumentMetadata("busy", {})])
        writer.execute("ROLLBACK")
        assert (
            reader.execute("SELECT 1 FROM metadata_documents WHERE document_id='busy'").fetchone()
            is None
        )


def test_palace_search_verbose_includes_ranks(
    clean_env: None,
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"only.md": "uniquetokenfox\n"},
    )
    rc = palace_main(
        [
            "search",
            "uniquetokenfox",
            "--store",
            str(tmp_store),
            "--mode",
            "bm25",
            "--no-rerank",
            "-v",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "rrf=" in out
    assert "bm25=#1" in out


def test_palace_search_empty_query_errors(
    clean_env: None,
    tmp_store: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # argparse won't let us pass an empty positional with nargs='+', but a single
    # whitespace token reaches cli_search and is rejected there.
    rc = palace_main(["search", "   ", "--store", str(tmp_store)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_search_resolves_relative_path_to_absolute_for_display(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    """``chunks.path`` is stored relative; search resolves it against ``watch_root``.

    The seeded chunk stores ``path="notes/a.md"`` and the absolute
    ``watch_root``; the returned :class:`SearchHit.path` is the resolved
    absolute ``str(<root> / "notes/a.md")``.
    """
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"notes/a.md": "## A\nresolvable token here\n"},
    )
    [hit] = search(query="resolvable", store=tmp_store, mode="bm25")
    assert hit.path == str(tmp_watch_root / "notes" / "a.md")
    assert hit.path.startswith("/")


def test_palace_search_fields_in_hit(
    tmp_store: Path, tmp_watch_root: Path, embedder: FakeEmbedder
) -> None:
    """SearchHit field shape is the API surface for downstream wrappers."""
    _seed(
        store=tmp_store,
        watch_root=tmp_watch_root,
        embedder=embedder,
        files={"x.md": "## H\nspecific token here\n"},
    )
    [hit] = search(query="specific", store=tmp_store, mode="bm25")
    assert isinstance(hit, SearchHit)
    assert hit.chunk_id
    assert hit.path.endswith("x.md")
    assert hit.heading == "H"
    assert hit.snippet
    assert hit.rrf_score > 0
    assert hit.bm25_rank == 1
    assert hit.bm25_score is not None and hit.bm25_score < 0  # FTS5 bm25 is negative
    assert hit.vec_rank is None
    assert hit.vec_distance is None
    assert hit.rerank_rank is None
    assert hit.rerank_score is None


def test_s3vectors_backend_query_composes_with_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store that selected its published S3 Vectors index searches through it."""
    import palace.index.s3vectors as s3v
    from palace.index._errors import IndexError as SearchError
    from palace.index.build import build
    from palace.index.config import chunks_db_path
    from palace.metadata import MetadataFilter
    from palace.multistore import search_stores
    from palace.search import search_reranked
    from palace.watch.config import WatchRootsConfig, default_config_path
    from tests.index.conftest import FakeS3Vectors

    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("PALACE_STORE", raising=False)
    embedder = FakeEmbedder()
    stores = {}
    for name in ("published", "local"):
        root = tmp_path / f"{name}-watch"
        root.mkdir()
        for i in range(5):
            (root / f"n{i}.md").write_text(f"# Note {i}\n\nlantern note {i}\n", encoding="utf-8")
        store = tmp_path / name
        store.mkdir()
        WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
        build(store=store, watch_root_filter=root, full=True, embedder=embedder)
        stores[name] = store
    store, local = stores["published"], stores["local"]
    fake = FakeS3Vectors()
    monkeypatch.setattr(s3v, "open_client", lambda profile, region: fake)
    s3v.save_publishing_config(store, note="fixture corpus, published to a fixture index")
    s3v.publish_vectors(
        store=store,
        bucket="fixture-vectors",
        index="fixture-index",
        region="us-east-1",
        profile="fixture",
        text_metadata=True,
    )

    # sqlite-vec stays the default: an unselected store makes no S3 Vectors request.
    fake.calls.clear()
    assert search(query="lantern", store=store, mode="vector", embedder=embedder)
    assert fake.calls == []
    assert (
        palace_main(
            ["index", "vector-backend", "set", "--store", str(store), "--backend", "s3vectors"]
        )
        == 0
    )
    assert palace_main(["index", "vector-backend", "show", "--store", str(store)]) == 0
    assert "backend: s3vectors" in capsys.readouterr().out

    # The leg never reads the local vector table; it returns the index's ranking, paged.
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        keys = sorted(row[0] for row in conn.execute("SELECT chunk_id FROM chunks_vec"))
        conn.execute("DELETE FROM chunks_vec")
        conn.commit()
    arn = fake.arn("fixture-vectors", "fixture-index")
    fake.vectors[arn]["remote-only"] = {
        "data": [0.0] * 4096,
        "metadata": {"text": "remote lantern passage", "path": "remote.md", "heading": "Remote"},
    }
    ranking = [(key, 0.1 * (i + 1)) for i, key in enumerate([*reversed(keys), "remote-only"])]
    fake.ranking = ranking
    fake.page_size = 2
    usage = s3v.VectorUsage()
    with s3v.vector_usage_scope(usage):
        hits = search(
            query="lantern", store=store, mode="vector", pool=5, limit=5, embedder=embedder
        )
    assert [(h.chunk_id, h.vec_distance) for h in hits] == ranking[:5]
    queries = [params for method, params in fake.calls if method == "query_vectors"]
    assert [q.get("nextToken") for q in queries] == [None, "2", "4"]
    assert all({**q, "nextToken": None} == {**queries[0], "nextToken": None} for q in queries)
    assert queries[0]["topK"] == 5 and queries[0]["returnMetadata"] is True
    records = [
        json.loads(line)
        for day in (store / "events" / "cloud-egress").glob("*.jsonl")
        for line in day.read_text().splitlines()
        if json.loads(line)["operation"] == "QueryVectors"
    ]
    assert len(records) == 3 and usage.summary().requests == 3
    assert usage.summary().request_bytes == sum(r["request_bytes"] for r in records)

    # Hybrid fusion ranks the index's matches beside BM25 exactly as before.
    lexical = {
        h.chunk_id: h.bm25_rank
        for h in search(query="lantern", store=store, mode="bm25", pool=10, limit=10)
    }
    fake.ranking = ranking[:5]
    hybrid = search(
        query="lantern", store=store, mode="hybrid", pool=10, limit=10, embedder=embedder
    )
    vector_rank = {key: i + 1 for i, (key, _) in enumerate(ranking[:5])}
    for hit in hybrid:
        expected = sum(
            1.0 / (RRF_K + rank)
            for rank in (lexical.get(hit.chunk_id), vector_rank.get(hit.chunk_id))
            if rank is not None
        )
        assert hit.rrf_score == pytest.approx(expected)

    # A match with no local row shows, reranks and merges from its published metadata.
    fake.ranking = [("remote-only", 0.05), *ranking[:2]]

    class Scorer:
        texts: list[str] = []

        def score(self, query: str, texts: Sequence[str]) -> list[float]:
            self.texts = list(texts)
            return [float("remote lantern passage" in text) for text in texts]

        def probe(self) -> None:
            pass

        def close(self) -> None:
            pass

    remote_hit = search(query="lantern", store=store, mode="vector", embedder=embedder)[0]
    assert (remote_hit.path, remote_hit.heading) == ("remote.md", "Remote")
    assert remote_hit.snippet == "remote lantern passage"
    scorer = Scorer()
    reranked = search_reranked(
        query="lantern", store=store, mode="vector", embedder=embedder, reranker=scorer
    )
    assert reranked.hits[0].chunk_id == "remote-only"
    assert any("remote lantern passage" in text for text in scorer.texts)
    assert reranked.vector_usage is not None and reranked.vector_usage.requests == 2
    before = len(fake.calls)
    merged = search_stores(
        query="lantern",
        stores=[store, local],
        mode="vector",
        embedders={resolve_identity(store): embedder},
        reranker=scorer,
    )
    assert merged.hits[0].hit.chunk_id == "remote-only" and merged.hits[0].store == store
    assert {item.store for item in merged.hits} == {store, local}
    assert merged.vector_usage is not None
    assert merged.vector_usage.requests == len(fake.calls) - before

    # A prepared query adopts a republished checkpoint but refuses a different index or
    # a different vector leg, before any query request.
    from palace.multistore import PreparedStoreSearch, execute_prepared, prepare_store_search

    def prepare_merged() -> PreparedStoreSearch:
        return prepare_store_search(
            query="lantern",
            stores=[store, local],
            mode="vector",
            embedders={resolve_identity(store): embedder},
            injected_reranker=True,
        )

    published = s3v.receipt_path(store)
    recorded = published.read_text()
    prepared = prepare_merged()
    checkpoint = json.loads(recorded)
    checkpoint["published_at"] = "2026-11-11T00:00:00+00:00"
    checkpoint["vector_count"] += 1
    published.write_text(json.dumps(checkpoint))
    before = len(fake.calls)
    adopted = execute_prepared(prepared, reranker=scorer)
    assert adopted.hits[0].hit.chunk_id == "remote-only" and len(fake.calls) > before
    prepared = prepare_merged()
    moved = json.loads(recorded)
    moved["index_arn"] += "-moved"
    published.write_text(json.dumps(moved))
    before = len(fake.calls)
    with pytest.raises(SearchError, match=r"binding changed for store .* \(published index\)"):
        execute_prepared(prepared, reranker=scorer)
    published.write_text(recorded)
    prepared = prepare_merged()
    selector = s3v.vector_backend_config_path(store)
    chosen = selector.read_text()
    selector.unlink()
    with pytest.raises(SearchError, match=r"binding changed for store .* \(vector backend\)"):
        execute_prepared(prepared, reranker=scorer)
    selector.write_text(chosen)
    assert len(fake.calls) == before

    # Deterministic index bounds refuse before any embedding or request, after the
    # publishing boundary.
    embeds = embedder.call_count
    published_only = {"stores": [store], "mode": "vector", "rerank_enabled": False}
    injected = {resolve_identity(store): embedder}
    with pytest.raises(SearchError, match=r"S3 Vectors top-K must be 1\.\.10000, not 10001"):
        prepare_store_search(query="lantern", pool=10_001, embedders=injected, **published_only)
    with pytest.raises(SearchError, match=r"S3 Vectors top-K must be 1\.\.10000, not 10001"):
        search_stores(query="lantern", pool=10_001, embedders=injected, **published_only)
    narrow = json.loads(recorded)
    narrow["dimension"] = 8
    published.write_text(json.dumps(narrow))
    with pytest.raises(SearchError, match="query vector has 4096 dimensions; the index holds 8"):
        search_stores(query="lantern", embedders=injected, **published_only)
    published.write_text(recorded)
    opt_in = s3v.publishing_config_path(store)
    opted = opt_in.read_bytes()
    opt_in.unlink()
    with pytest.raises(SearchError, match="not opted in as published"):
        prepare_store_search(query="lantern", pool=10_001, embedders=injected, **published_only)
    opt_in.write_bytes(opted)
    assert embedder.call_count == embeds and len(fake.calls) == before

    # Refusals before any query: metadata filters, a mismatched or interrupted receipt.
    receipt_path = store / "meta" / "vector-publication.json"
    original = receipt_path.read_text()
    before = len(fake.calls)
    with pytest.raises(SearchError, match="metadata filters are not supported"):
        search(
            query="lantern",
            store=store,
            mode="vector",
            embedder=embedder,
            filters=[MetadataFilter("tag", any_of=["x"])],
        )
    receipt_path.write_text(original.replace('"qwen3-embedding:8b"', '"other-model"'))
    with pytest.raises(SearchError, match="vector publication identity mismatch: model"):
        search(query="lantern", store=store, mode="vector", embedder=embedder)
    receipt_path.write_text(original.replace('"complete"', '"publishing"'))
    with pytest.raises(SearchError, match="last publish did not complete"):
        search_stores(
            query="lantern",
            stores=[store, local],
            mode="vector",
            embedders={resolve_identity(store): embedder},
        )
    receipt_path.unlink()
    with pytest.raises(SearchError, match="no publication receipt"):
        search(query="lantern", store=store, mode="hybrid", embedder=embedder)
    assert len(fake.calls) == before
    receipt_path.write_text(original)

    # The command line keeps hit lines on stdout and reports usage on stderr.
    monkeypatch.setattr("palace.search.OllamaEmbedder", lambda: FakeEmbedder())
    capsys.readouterr()
    argv = ["search", "lantern", "--store", str(store), "--mode", "vector", "--no-rerank"]
    assert palace_main([*argv, "--json"]) == 0
    out = capsys.readouterr()
    assert all("chunk_id" in json.loads(line) for line in out.out.splitlines())
    assert json.loads(out.err.strip().splitlines()[-1])["vector_usage"]["requests"] == 2
    assert palace_main(argv) == 0
    assert "palace search: vector backend s3vectors requests=2" in capsys.readouterr().err


def test_bedrock_store_builds_and_searches_at_its_own_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from palace.index._errors import IndexError as PalaceError
    from palace.index.build import build
    from palace.index.config import chunks_db_path, prepare_chunks_db_path
    from palace.index.embedder_config import EmbedderConfig, save_embedder_config
    from palace.index.schema import _ddl_statements, declared_vector_dim, init_db, read_identity
    from tests.test_bedrock_provider import BedrockStub, audit, fixture_credentials

    stub = BedrockStub()
    stub.install(monkeypatch)
    fixture_credentials(monkeypatch)
    root = tmp_path / "published"
    root.mkdir()
    files = {
        "alpha.md": "## Alpha\nlantern harbor\n",
        "beta.md": "## Beta\nquiet meadow\n",
        "gamma.md": "## Gamma\ncopper kettle\n",
    }
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")

    def selector(dim: int, *, published: bool = True) -> EmbedderConfig:
        return EmbedderConfig(
            provider="bedrock",
            model="cohere.embed-v4:0",
            dim=dim,
            region="us-east-1",
            published_corpus=published,
            published_corpus_note="Synthetic published fixture" if published else None,
            published_corpus_asserted_at="2026-11-07T00:00:00Z" if published else None,
        )

    def vec_sql(conn: sqlite3.Connection) -> str:
        return str(
            conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'").fetchone()[0]
        )

    def vec_rows(store: Path) -> list[tuple[str, bytes]]:
        import sqlite_vec

        with closing(sqlite3.connect(chunks_db_path(store))) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            return [
                (str(cid), bytes(blob))
                for cid, blob in conn.execute(
                    "SELECT chunk_id, embedding FROM chunks_vec ORDER BY chunk_id"
                )
            ]

    store = tmp_path / "store"
    save_embedder_config(store, selector(1536))
    build(store=store, watch_root_filter=root, full=True, log=lambda _line: None)
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        assert "embedding FLOAT[1536]" in vec_sql(conn)
        assert declared_vector_dim(conn) == 1536
        recorded = read_identity(conn)
    assert (recorded.provider, recorded.model, recorded.dim) == (
        "bedrock:us-east-1",
        "cohere.embed-v4:0",
        "1536",
    )
    build_types = {json.loads(r.body)["input_type"] for r in stub.invokes()}
    assert build_types == {"search_document"}
    assert sorted(len(blob) for _, blob in vec_rows(store)) == [1536 * 4] * 3

    hits = search(query="lantern harbor", store=store, mode="vector")
    assert hits[0].path.endswith("alpha.md")
    assert json.loads(stub.invokes()[-1].body)["input_type"] == "search_query"

    # Without a published-corpus assertion the build refuses before any request.
    unpublished = tmp_path / "unpublished"
    save_embedder_config(unpublished, selector(1536, published=False))
    before = len(stub.requests)
    with pytest.raises(PalaceError, match="published-corpus assertion"):
        build(store=unpublished, watch_root_filter=root, full=True, log=lambda _line: None)
    assert len(stub.requests) == before

    # A different selected width refuses before the probe: no request of any kind, no new
    # audit record and unchanged vectors; search refuses on identity.
    rows, records = vec_rows(store), audit(store)
    save_embedder_config(store, selector(1024))
    before = len(stub.requests)
    with pytest.raises(PalaceError, match="chunks vector table width 1536 differs"):
        build(store=store, watch_root_filter=root, full=True, log=lambda _line: None)
    assert len(stub.requests) == before
    assert audit(store) == records and vec_rows(store) == rows
    with pytest.raises(PalaceError, match="embedding-identity mismatch: dim found='1536'"):
        search(query="lantern harbor", store=store, mode="vector")
    assert len(stub.requests) == before

    # A default store's vector table is the literal pre-phase 4096 statement.
    import sqlite_vec

    default = tmp_path / "default"
    default.mkdir()
    with closing(sqlite3.connect(str(prepare_chunks_db_path(default)))) as conn:
        init_db(conn, store=default)
        stored = vec_sql(conn)
    pre_phase = """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[4096]
        )
        """
    assert pre_phase in _ddl_statements(4096)
    with closing(sqlite3.connect(":memory:")) as scratch:
        scratch.enable_load_extension(True)
        sqlite_vec.load(scratch)
        scratch.execute(pre_phase)
        assert vec_sql(scratch) == stored

    # A relative store path builds twice through the pre-probe width check (the second
    # build reads the existing database), and still refuses a different width before any
    # provider request.
    monkeypatch.chdir(tmp_path)
    relative = Path("relative-store")
    for _ in range(2):
        build(
            store=relative,
            watch_root_filter=root,
            full=True,
            embedder=FakeEmbedder(),
            log=lambda _line: None,
        )
    save_embedder_config(relative, selector(1024))
    before = len(stub.requests)
    with pytest.raises(PalaceError, match="chunks vector table width 4096 differs"):
        build(store=relative, watch_root_filter=root, full=True, log=lambda _line: None)
    assert len(stub.requests) == before


def _assert_supplied_search(store: Path, root: Path, embedder: FakeEmbedder) -> None:
    import sqlite_vec

    from palace.index import (
        EmbeddingRepresentation,
        commit_plan,
        init_db,
        plan_one,
        with_embedding_inputs,
        writer_lock,
    )
    from palace.index._errors import IndexError as PalaceIndexError
    from palace.index.config import EMBED_DIM, prepare_chunks_db_path
    from palace.index.embedder_config import EmbedderConfig, save_embedder_config

    representation = EmbeddingRepresentation("sections-v1", "fixture-v1", "md-v1", "heading-v1")
    save_embedder_config(
        store,
        EmbedderConfig("ollama", "qwen3-embedding:8b", EMBED_DIM, representation=representation),
    )
    with (
        writer_lock(store, operation="fixture", timeout=0),
        closing(sqlite3.connect(prepare_chunks_db_path(store), isolation_level=None)) as conn,
    ):
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        init_db(conn, store=store)
        note = root / "supplied.md"
        note.write_text("independent literal body\n\n# Display name\n")
        plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=root,
            full=True,
            log=lambda _: None,
        )
        plan = with_embedding_inputs(
            plan, representation=representation, inputs=[row.body for row in plan.rows]
        )
        commit_plan(
            conn=conn,
            plan=plan,
            embeddings=embedder.embed(plan.embed_inputs),
            identity=resolve_identity(store),
            log=lambda _: None,
        )
    hits = search(
        query="independent literal body",
        store=store,
        mode="vector",
        embedder=embedder,
    )
    assert hits and Path(hits[0].path) == note
    assert embedder.last_batch == ["independent literal body"]
    before = embedder.call_count
    save_embedder_config(store, EmbedderConfig("ollama", "qwen3-embedding:8b", EMBED_DIM))
    with pytest.raises(PalaceIndexError, match="embedding-identity mismatch"):
        search(
            query="independent literal body",
            store=store,
            mode="vector",
            embedder=embedder,
        )
    assert embedder.call_count == before
