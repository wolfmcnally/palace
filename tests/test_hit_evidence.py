"""Proofs of returned-hit evidence on the single- and multi-store prepared seams.

Expected entries are read from each store before the search. A commit from a second
connection lands after retrieval returns and before evidence is collected, inside the
retrieval's read snapshot, so evidence read from any later transaction would differ.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import struct
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

import palace.multistore as multistore_module
import palace.search as search_module
from palace.index._errors import IndexError as SearchError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path, prepare_chunks_db_path
from palace.index.embedder_config import EmbedderConfig, resolve_identity, save_embedder_config
from palace.index.enrich import scored_text
from palace.index.representation import input_digest
from palace.metadata import MetadataFilter
from palace.multistore import prepare_store_search, search_stores
from palace.prepared import HitEvidence, HitEvidenceStatus
from palace.rerank import RerankUnavailableError
from palace.search import execute_prepared_search, prepare_search
from palace.watch.config import WatchRootsConfig, default_config_path
from tests.index.conftest import FakeEmbedder, FakeS3Vectors

_NOTES = {
    "alpha.md": "## Alpha\nlantern harbor at dusk\n",
    "beta.md": "## Beta\nlantern meadow notes\n",
    "gamma.md": "## Gamma\ncopper lantern kettle\n",
}


class Scorer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if self.fail:
            raise RerankUnavailableError("fixture reranker unavailable")
        return [float(len(text) % 5) for text in texts]

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("PALACE_STORE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    yield tmp_path


def _built(tmp_path: Path, name: str) -> Path:
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    for file_name, body in _NOTES.items():
        (root / file_name).write_text(body, encoding="utf-8")
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder())
    return store


def _supplied(tmp_path: Path) -> Path:
    """A store whose explicit representation records each chunk's embedding input."""
    from palace.index import (
        EmbeddingRepresentation,
        commit_plan,
        init_db,
        plan_one,
        with_embedding_inputs,
        writer_lock,
    )

    root = tmp_path / "supplied-watch"
    root.mkdir()
    store = tmp_path / "supplied"
    representation = EmbeddingRepresentation("sections-v1", "fixture-v1", "md-v1", "heading-v1")
    save_embedder_config(
        store,
        EmbedderConfig("ollama", "qwen3-embedding:8b", EMBED_DIM, representation=representation),
    )
    embedder = FakeEmbedder()
    with (
        writer_lock(store, operation="fixture", timeout=0),
        closing(sqlite3.connect(prepare_chunks_db_path(store), isolation_level=None)) as conn,
    ):
        _load(conn)
        init_db(conn, store=store)
        for file_name, body in _NOTES.items():
            note = root / file_name
            note.write_text(body, encoding="utf-8")
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
                plan, representation=representation, inputs=[f"input {r.body}" for r in plan.rows]
            )
            commit_plan(
                conn=conn,
                plan=plan,
                embeddings=embedder.embed(plan.embed_inputs),
                identity=resolve_identity(store),
                log=lambda _: None,
            )
    return store


def _load(conn: sqlite3.Connection) -> None:
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _write(store: Path, *statements: tuple[str, tuple[object, ...]]) -> None:
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        _load(conn)
        for sql, parameters in statements:
            conn.execute(sql, parameters)
        conn.commit()


def _stored(store: Path, evidence_store: Path | None = None) -> dict[str, HitEvidence]:
    """Every chunk's verified evidence, read before any search."""
    identity = resolve_identity(store)
    with closing(sqlite3.connect(chunks_db_path(store))) as conn:
        _load(conn)
        supplied = {
            row[0]: row[1]
            for row in conn.execute("SELECT chunk_id, input_sha256 FROM chunk_embedding_inputs")
        }
        rows = conn.execute(
            "SELECT c.chunk_id, c.watch_root, c.path, f.file_hash, c.section_index, "
            "c.window_index, c.heading, c.body, c.body_hash, v.embedding FROM chunks c "
            "JOIN files f USING (watch_root, path) JOIN chunks_vec v USING (chunk_id)"
        ).fetchall()
    stored = {}
    for cid, root, path, file_hash, section, window, heading, body, body_hash, blob in rows:
        derived = input_digest(scored_text(path=path, heading=heading, body=body))
        digest = supplied.get(cid, derived)
        stored[cid] = HitEvidence(
            evidence_store or store,
            cid,
            "verified",
            identity,
            root,
            path,
            file_hash,
            section,
            window,
            body,
            body_hash,
            digest,
            bytes(blob),
        )
    return stored


def _tampering(stores: Sequence[Path], original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a retrieval call so a second connection commits right after it returns."""

    def wrapped(conn: sqlite3.Connection, **kwargs: Any) -> Any:
        result = original(conn, **kwargs)
        for store in stores:
            _write(
                store,
                ("UPDATE chunks SET body = body || ' changed', body_hash = 'f' || body_hash", ()),
                ("UPDATE files SET file_hash = 'f' || file_hash", ()),
            )
        return result

    return wrapped


def _without(result: Any) -> Any:
    return replace(result, evidence=None)


@pytest.mark.parametrize("seam", ["single-plain", "single-reranked", "multi-store"])
def test_hit_evidence_attests_returned_hits_in_the_retrieval_snapshot(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    pristine = _built(isolated, "pristine")

    def fresh(name: str) -> Path:
        copy = isolated / name
        shutil.copytree(pristine, copy)
        return copy

    embedder = FakeEmbedder()
    query = {"query": "lantern", "mode": "hybrid", "pool": 10}

    if seam == "single-plain":
        store = fresh("plain")
        expected = _stored(store)
        plain = execute_prepared_search(
            prepare_search(store=store, rerank_enabled=False, embedder=embedder, **query)
        )
        prepared = prepare_search(
            store=store, rerank_enabled=False, embedder=embedder, evidence=True, **query
        )
        monkeypatch.setattr(
            search_module, "_search_conn", _tampering([store], search_module._search_conn)
        )
        attested = execute_prepared_search(prepared)
        assert attested.status == "disabled" and attested.hits
        assert _without(attested) == plain and plain.evidence is None
        assert attested.evidence == tuple(expected[hit.chunk_id] for hit in attested.hits)
        assert _stored(store) != expected  # the second connection did commit

        # Evidence needs a vector leg: bm25 refuses before any provider call.
        calls = embedder.call_count
        for prepare in (
            lambda: prepare_search(query="lantern", store=store, mode="bm25", evidence=True),
            lambda: prepare_store_search(
                query="lantern", stores=[store], mode="bm25", evidence=True, multi_query=2
            ),
            lambda: search_stores(query="lantern", stores=[store], mode="bm25", evidence=True),
        ):
            with pytest.raises(SearchError, match="hit evidence requires .* mode=bm25"):
                prepare()
        assert embedder.call_count == calls
        return

    if seam == "single-reranked":
        cases: list[tuple[str, dict[str, Any]]] = [
            ("applied", {"reranker": Scorer()}),
            ("failed", {"reranker": Scorer(fail=True), "strict": False}),
            ("applied", {"reranker": Scorer(), "filters": [MetadataFilter("tag", any_of=["no"])]}),
        ]
        for index, (status, options) in enumerate(cases):
            store = fresh(f"reranked-{index}")
            expected = _stored(store)
            reranker = options.pop("reranker")
            arguments = {**query, "limit": 2, "rerank_in": 3, "injected_reranker": True, **options}
            plain = execute_prepared_search(
                prepare_search(store=store, embedder=embedder, **arguments), reranker=reranker
            )
            prepared = prepare_search(store=store, embedder=embedder, evidence=True, **arguments)
            with monkeypatch.context() as patch:
                patch.setattr(
                    search_module,
                    "_fuse_variants",
                    _tampering([store], search_module._fuse_variants),
                )
                attested = execute_prepared_search(prepared, reranker=reranker)
            assert attested.status == status and _without(attested) == plain
            assert attested.evidence == tuple(expected[hit.chunk_id] for hit in attested.hits)
            assert bool(attested.hits) == ("filters" not in options)
        return

    for index, (status, options) in enumerate(
        [
            ("applied", {"reranker": Scorer()}),
            ("disabled", {"rerank_enabled": False}),
            ("failed", {"reranker": Scorer(fail=True), "strict": False}),
            ("applied", {"reranker": Scorer(), "empty": True}),
        ]
    ):
        stores = [fresh(f"multi-{index}-a"), fresh(f"multi-{index}-b")]
        if options.pop("empty", False):
            for store in stores:
                _write(
                    store,
                    ("DELETE FROM chunks_vec", ()),
                    ("DELETE FROM chunks_fts", ()),
                    ("DELETE FROM chunks", ()),
                )
        expected = {
            (store.resolve(), cid): entry
            for store in stores
            for cid, entry in _stored(store, store.resolve()).items()
        }
        arguments = {**query, "stores": stores, "limit": 4, "rerank_in": 3, **options}
        identity = {resolve_identity(stores[0]): embedder}
        plain = search_stores(embedders=identity, **arguments)
        with monkeypatch.context() as patch:
            patch.setattr(
                multistore_module,
                "_fuse_variants",
                _tampering(stores, multistore_module._fuse_variants),
            )
            attested = search_stores(embedders=identity, evidence=True, **arguments)
        assert attested.status == status and _without(attested) == plain
        assert attested.evidence == tuple(
            expected[(item.store, item.hit.chunk_id)] for item in attested.hits
        )
        assert bool(attested.hits) == bool(expected)
        if attested.hits:
            assert {item.store for item in attested.hits} == {store.resolve() for store in stores}


def _blank(entry: HitEvidence, status: HitEvidenceStatus) -> HitEvidence:
    return replace(
        entry, status=status, body=None, body_hash=None, input_sha256=None, vector_f32=None
    )


@pytest.mark.parametrize(
    "damage",
    [
        "body-hash-mismatch",
        "missing-vector",
        "malformed-vector",
        "missing-input-digest",
        "remote-only",
    ],
)
def test_hit_evidence_isolates_one_damaged_entry(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    embedder = FakeEmbedder()
    if damage == "remote-only":
        _assert_remote_only(isolated, monkeypatch, embedder)
        return
    store = _supplied(isolated) if damage == "missing-input-digest" else _built(isolated, "store")
    expected = _stored(store)
    target = next(cid for cid, entry in expected.items() if entry.path == "alpha.md")
    statement = {
        "body-hash-mismatch": ("UPDATE chunks SET body_hash = ? WHERE chunk_id = ?", ("0" * 64,)),
        "missing-vector": ("DELETE FROM chunks_vec WHERE chunk_id = ?", ()),
        "malformed-vector": (
            "UPDATE chunks_vec SET embedding = ? WHERE chunk_id = ?",
            # Not NaN: a NaN-distance KNN row fails retrieval itself, before evidence.
            (struct.pack(f"<{EMBED_DIM}f", math.inf, *[0.5] * (EMBED_DIM - 1)),),
        ),
        "missing-input-digest": ("DELETE FROM chunk_embedding_inputs WHERE chunk_id = ?", ()),
    }[damage]
    _write(store, (statement[0], (*statement[1], target)))
    if damage == "body-hash-mismatch":
        # Precedence: the body check precedes the vector checks, so a missing vector on
        # the same chunk still reports body-hash-mismatch.
        _write(store, ("DELETE FROM chunks_vec WHERE chunk_id = ?", (target,)))
    orphans: dict[str, HitEvidence] = {}
    if damage == "missing-vector":
        # The converse damage: a vector whose chunk row is gone. Evidence off returns it
        # as a hit; evidence on reports it with no invented local identity.
        orphan_vector = sqlite_vec.serialize_float32(embedder.embed(["lantern"])[0])
        _write(store, ("INSERT INTO chunks_vec VALUES (?, ?)", ("orphan-vector", orphan_vector)))
        orphans["orphan-vector"] = HitEvidence(
            store, "orphan-vector", "missing-local-row", resolve_identity(store), *[None] * 5
        )
    arguments = {"query": "lantern", "mode": "hybrid", "pool": 10, "limit": 10}
    plain = execute_prepared_search(
        prepare_search(store=store, rerank_enabled=False, embedder=embedder, **arguments)
    )
    attested = execute_prepared_search(
        prepare_search(
            store=store, rerank_enabled=False, embedder=embedder, evidence=True, **arguments
        )
    )
    assert _without(attested) == plain
    assert {hit.chunk_id for hit in attested.hits} == set(expected) | set(orphans)
    assert attested.evidence == tuple(
        orphans[hit.chunk_id]
        if hit.chunk_id in orphans
        else _blank(expected[hit.chunk_id], damage)  # type: ignore[arg-type]
        if hit.chunk_id == target
        else expected[hit.chunk_id]
        for hit in attested.hits
    )


def _assert_remote_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: FakeEmbedder
) -> None:
    import palace.index.s3vectors as s3v

    published, local = _built(tmp_path, "published"), _built(tmp_path, "local")
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
    stored = _stored(published, published.resolve())
    kept = sorted(stored)[0]
    # Precedence: remote-only precedes every local check, even a damaged body hash.
    _write(published, ("UPDATE chunks SET body_hash = ? WHERE chunk_id = ?", ("0" * 64, kept)))
    arn = fake.arn("fixture-vectors", "fixture-index")
    fake.vectors[arn]["no-local-row"] = {"data": [0.0] * EMBED_DIM, "metadata": {}}

    # The published store's one hit is reported, never requested; local hits verify.
    fake.ranking = [(kept, 0.1)]
    arguments = {
        "query": "lantern",
        "stores": [published, local],
        "mode": "vector",
        "embedders": {resolve_identity(local): embedder},
        "rerank_enabled": False,
    }
    before = len(fake.calls)
    plain = search_stores(**arguments)
    spent = len(fake.calls) - before
    attested = search_stores(evidence=True, **arguments)
    assert len(fake.calls) - before == 2 * spent and spent > 0
    assert _without(attested) == plain
    locals_ = _stored(local, local.resolve())
    assert attested.evidence == tuple(
        _blank(stored[item.hit.chunk_id], "remote-only")
        if item.store == published.resolve()
        else locals_[item.hit.chunk_id]
        for item in attested.hits
    )
    assert [item.hit.chunk_id for item in attested.hits if item.store == published.resolve()] == [
        kept
    ]

    # A remote hit with no local row and no published text: identifying fields are None,
    # and the no-candidates rerank failure still carries its entry.
    fake.ranking = [("no-local-row", 0.05)]
    single = {"query": "lantern", "store": published, "mode": "vector", "embedder": embedder}
    failed = execute_prepared_search(
        prepare_search(evidence=True, injected_reranker=True, strict=False, **single),
        reranker=Scorer(),
    )
    assert failed.status == "failed"
    assert failed.detail == "reranker candidate hydration returned no candidates"
    identity = resolve_identity(published)
    absent = HitEvidence(
        published, "no-local-row", "remote-only", identity, None, None, None, None, None
    )
    assert failed.evidence == (absent,)
    shown = execute_prepared_search(prepare_search(evidence=True, rerank_enabled=False, **single))
    assert shown.evidence == (absent,)
