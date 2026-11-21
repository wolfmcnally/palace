"""Proofs of opt-in distinct results on the single- and multi-store prepared seams.

Each note is named ``<group>-<member>.md``; the fixture keys hits by group. The
scorer's score for a candidate comes from a table keyed by note name, so the rerank
order is chosen by the test, independently of the fused order.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import palace.multistore as multistore_module
import palace.search as search_module
from palace.index._errors import IndexError as SearchError
from palace.index.build import build
from palace.index.embedder_config import resolve_identity
from palace.metadata import MetadataFilter
from palace.multistore import StoreHit, prepare_store_search
from palace.prepared import DistinctOutcome, DistinctResults, HitEvidence
from palace.rerank import RerankUnavailableError
from palace.search import SearchHit, execute_prepared_search, prepare_search
from palace.vector_cache import VectorCache
from palace.watch.config import WatchRootsConfig, default_config_path
from tests.index.conftest import FakeEmbedder
from tests.test_hit_evidence import _stored, _tampering
from tests.test_prepared_search import RecordingExpander

_NAME = re.compile(r"(g\d\d-\w+)\.md")


class TableScorer:
    """Scores each candidate from a table keyed by note name and records every call."""

    def __init__(self, table: Mapping[str, float] | None = None, *, fail: bool = False) -> None:
        self.table = dict(table or {})
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        if self.fail:
            raise RerankUnavailableError("fixture reranker unavailable")
        scores = []
        for text in texts:
            match = _NAME.search(text)
            assert match is not None, text
            scores.append(self.table.get(match.group(1), 0.0))
        return scores

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


def _store(tmp_path: Path, name: str, notes: Sequence[str]) -> Path:
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    for note in notes:
        (root / f"{note}.md").write_text(f"## {note}\nlantern {note} notes\n", encoding="utf-8")
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder())
    return store


def _note(hit: SearchHit | StoreHit) -> str:
    path = hit.hit.path if isinstance(hit, StoreHit) else hit.path
    return Path(path).stem


def _group(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
    return _note(hit).split("-")[0]


def _unique(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
    return _note(hit)


def _single(store: Path, **options: Any) -> search_module.PreparedSearch:
    arguments: dict[str, Any] = {"query": "lantern", "mode": "hybrid", "pool": 50}
    arguments.update(options)
    return prepare_search(store=store, embedder=FakeEmbedder(), **arguments)


_EARLY = ["g01-a", "g01-b", "g01-c", "g01-d"] + [f"g{n:02d}-a" for n in range(2, 13)]


def test_ten_requested_past_early_duplicates_return_ten_from_the_same_rerank_call(
    isolated: Path,
) -> None:
    store = _store(isolated, "early", _EARLY)
    scores = {note: 100.0 - index for index, note in enumerate(_EARLY)}
    embedder, expander = FakeEmbedder(), RecordingExpander()
    prepared = prepare_search(
        query="lantern",
        store=store,
        mode="hybrid",
        pool=50,
        limit=5,
        rerank_in=20,
        injected_reranker=True,
        hyde=True,
        embedder=embedder,
        expander=expander,
    )
    assert prepared.route == "reranked"
    prepared_calls = (embedder.call_count, list(embedder.all_batches), list(expander.calls))

    plain_scorer = TableScorer(scores)
    plain = execute_prepared_search(prepared, reranker=plain_scorer)
    scorer = TableScorer(scores)
    grouped = execute_prepared_search(
        prepared, reranker=scorer, distinct=DistinctResults(_group, 10)
    )
    # Execution embeds and expands nothing, with or without grouping.
    assert (embedder.call_count, embedder.all_batches, expander.calls) == prepared_calls

    assert plain.status == grouped.status == "applied" and len(plain.hits) == 5
    assert plain.distinct is None
    assert [_note(hit) for hit in grouped.hits] == ["g01-a"] + [f"g{n:02d}-a" for n in range(2, 11)]
    assert grouped.distinct == DistinctOutcome(15, 13, 0, 3, 10, False)
    # One score call over exactly the admitted candidates, with and without grouping.
    assert len(scorer.calls) == len(plain_scorer.calls) == 1
    assert scorer.calls == plain_scorer.calls and len(scorer.calls[0][1]) == 15
    # The grouped hits are the plain ranking's hits, rank fields included.
    assert grouped.hits[:2] == [plain.hits[0], plain.hits[4]]
    assert [hit.rerank_rank for hit in grouped.hits] == [1, *range(5, 14)]

    # A pool that genuinely holds seven distinct keys returns seven, underfilled.
    def seven(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
        group = int(_group(hit, evidence)[1:])
        return f"g{min(group, 7):02d}"

    short = execute_prepared_search(
        prepared, reranker=TableScorer(scores), distinct=DistinctResults(seven, 10)
    )
    assert [_note(hit) for hit in short.hits] == ["g01-a"] + [f"g{n:02d}-a" for n in range(2, 8)]
    assert short.distinct == DistinctOutcome(15, 15, 0, 8, 7, True)


def test_an_ineligible_stale_head_yields_to_the_reranked_sibling_not_the_fused_winner(
    isolated: Path,
) -> None:
    notes = ["g01-stale", "g01-one", "g01-two"] + [f"g{n:02d}-a" for n in range(2, 8)]
    store = _store(isolated, "stale", notes)
    prepared = _single(store, limit=3, rerank_in=20, injected_reranker=True, strict=False)

    # A failed rerank pools every admitted candidate in fused order.
    fused = execute_prepared_search(
        prepared, reranker=TableScorer(fail=True), distinct=DistinctResults(_unique, 20)
    )
    assert fused.status == "failed" and len(fused.hits) == len(notes)
    order = [_note(hit) for hit in fused.hits]
    fused_winner, fused_loser = sorted(["g01-one", "g01-two"], key=order.index)

    scores = {"g01-stale": 100.0, fused_loser: 80.0, fused_winner: 50.0}
    scores.update({f"g{n:02d}-a": 70.0 - n for n in range(2, 8)})

    def eligible(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str | None:
        return None if _note(hit).endswith("stale") else _group(hit, evidence)

    grouped = execute_prepared_search(
        prepared, reranker=TableScorer(scores), distinct=DistinctResults(eligible, 4)
    )
    assert grouped.status == "applied"
    assert [_note(hit) for hit in grouped.hits] == [fused_loser, "g02-a", "g03-a", "g04-a"]
    assert grouped.distinct == DistinctOutcome(len(notes), 5, 1, 0, 4, False)


def test_every_route_pools_what_it_already_produced_and_ties_keep_rerank_order(
    isolated: Path,
) -> None:
    notes = [f"g{n:02d}-{member}" for n in range(1, 5) for member in ("a", "b")]
    store = _store(isolated, "routes", notes)
    grouping = DistinctResults(_group, 10)

    # Ties: a constant score ranks by chunk id; the walk keeps that order.
    tied = execute_prepared_search(
        _single(store, limit=2, rerank_in=20, injected_reranker=True),
        reranker=TableScorer(),
        distinct=grouping,
    )
    pool = execute_prepared_search(
        _single(store, limit=8, rerank_in=20, injected_reranker=True), reranker=TableScorer()
    )
    assert [hit.chunk_id for hit in pool.hits] == sorted(hit.chunk_id for hit in pool.hits)
    first: dict[str, SearchHit] = {}
    for hit in pool.hits:
        first.setdefault(_group(hit, None), hit)
    assert tied.hits == list(first.values())
    assert tied.distinct == DistinctOutcome(8, 8, 0, 4, 4, True)

    # Direct and fused routes pool the hits they already return: the prepared limit.
    direct = _single(store, limit=3, rerank_enabled=False)
    expander = RecordingExpander()
    expanded = _single(store, limit=3, rerank_enabled=False, hyde=True, expander=expander)
    for prepared, route in ((direct, "direct"), (expanded, "fused")):
        assert prepared.route == route
        released = execute_prepared_search(prepared)
        grouped = execute_prepared_search(prepared, distinct=DistinctResults(_unique, 10))
        assert grouped.hits == released.hits and len(released.hits) == 3
        assert grouped.distinct == DistinctOutcome(3, 3, 0, 0, 3, True)
        assert replace(grouped, distinct=None) == released

    # An empty pool calls no key and reports an underfilled result.
    def never(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
        raise AssertionError("no hit to key")

    empty = execute_prepared_search(
        _single(store, injected_reranker=True, filters=[MetadataFilter("tag", any_of=["no"])]),
        reranker=TableScorer(),
        distinct=DistinctResults(never, 3),
    )
    assert empty.hits == [] and empty.distinct == DistinctOutcome(0, 0, 0, 0, 0, True)


@pytest.mark.parametrize("seam", ["single-store", "multi-store"])
def test_pool_evidence_is_read_in_the_retrieval_snapshot_and_reaches_the_key(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    notes = [f"g{n:02d}-{member}" for n in range(1, 5) for member in ("a", "b")]
    pristine = _store(isolated, "pristine", notes)
    seen: list[tuple[str, HitEvidence | None]] = []

    def recording(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
        chunk_id = hit.hit.chunk_id if isinstance(hit, StoreHit) else hit.chunk_id
        seen.append((chunk_id, evidence))
        return _group(hit, evidence)

    if seam == "single-store":
        cases: list[tuple[str, dict[str, Any]]] = [
            ("applied", {"reranker": TableScorer()}),
            ("failed", {"reranker": TableScorer(fail=True), "strict": False}),
        ]
        for index, (status, options) in enumerate(cases):
            store = isolated / f"single-{index}"
            shutil.copytree(pristine, store)
            expected = _stored(store)
            reranker = options.pop("reranker")
            prepared = _single(
                store, limit=2, rerank_in=20, injected_reranker=True, evidence=True, **options
            )
            seen.clear()
            with monkeypatch.context() as patch:
                patch.setattr(
                    search_module,
                    "_fuse_variants",
                    _tampering([store], search_module._fuse_variants),
                )
                grouped = execute_prepared_search(
                    prepared, reranker=reranker, distinct=DistinctResults(recording, 4)
                )
            assert grouped.status == status and len(grouped.hits) == 4  # beyond limit 2
            assert grouped.evidence == tuple(expected[hit.chunk_id] for hit in grouped.hits)
            assert seen and all(entry == expected[cid] for cid, entry in seen)
            assert _stored(store) != expected  # the second connection did commit
        # Without prepared evidence the key receives None.
        seen.clear()
        bare = execute_prepared_search(
            _single(pristine, limit=2, injected_reranker=True),
            reranker=TableScorer(),
            distinct=DistinctResults(recording, 4),
        )
        assert bare.evidence is None and seen and all(entry is None for _, entry in seen)
        return

    for index, (status, options) in enumerate(
        [
            ("applied", {"reranker": TableScorer()}),
            ("disabled", {"rerank_enabled": False}),
            ("failed", {"reranker": TableScorer(fail=True), "strict": False}),
        ]
    ):
        stores = [isolated / f"multi-{index}-a", isolated / f"multi-{index}-b"]
        for store in stores:
            shutil.copytree(pristine, store)
        expected = {
            (store.resolve(), cid): entry
            for store in stores
            for cid, entry in _stored(store, store.resolve()).items()
        }
        reranker = options.pop("reranker", None)
        prepared = prepare_store_search(
            query="lantern",
            stores=stores,
            pool=50,
            limit=2,
            rerank_in=20,
            embedders={resolve_identity(stores[0]): FakeEmbedder()},
            injected_reranker=reranker is not None,
            evidence=True,
            **options,
        )
        seen.clear()
        with monkeypatch.context() as patch:
            patch.setattr(
                multistore_module,
                "_fuse_variants",
                _tampering(stores, multistore_module._fuse_variants),
            )
            grouped = multistore_module.execute_prepared(
                prepared, reranker=reranker, distinct=DistinctResults(_store_note, 10)
            )
        assert grouped.status == status and len(grouped.hits) == 10  # beyond limit 2
        assert grouped.evidence == tuple(
            expected[(item.store, item.hit.chunk_id)] for item in grouped.hits
        )
        assert {item.store for item in grouped.hits} == {store.resolve() for store in stores}


def _store_note(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
    assert isinstance(hit, StoreHit) and evidence is not None
    assert (evidence.store, evidence.chunk_id) == (hit.store, hit.hit.chunk_id)
    return json.dumps([str(hit.store), _note(hit)])


def test_cross_store_identity_is_the_callers_key_and_the_score_call_is_unchanged(
    isolated: Path,
) -> None:
    notes = [f"g{n:02d}-a" for n in range(1, 6)]
    pristine = _store(isolated, "pristine", notes)
    stores = [isolated / "left", isolated / "right"]
    for store in stores:
        shutil.copytree(pristine, store)
    scores = {note: 10.0 - index for index, note in enumerate(notes)}
    embedder, expander = FakeEmbedder(), RecordingExpander()
    prepared = prepare_store_search(
        query="lantern",
        stores=stores,
        pool=50,
        limit=3,
        rerank_in=20,
        embedders={resolve_identity(stores[0]): embedder},
        expander=expander,
        multi_query=2,
        injected_reranker=True,
    )
    prepared_calls = (embedder.call_count, list(embedder.all_batches), list(expander.calls))
    plain_scorer = TableScorer(scores)
    plain = multistore_module.execute_prepared(prepared, reranker=plain_scorer)
    assert len(plain.hits) == 3 and plain.distinct is None

    def by_origin(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
        assert isinstance(hit, StoreHit)
        return json.dumps(hit.origin_id)

    collapsed_scorer = TableScorer(scores)
    collapsed = multistore_module.execute_prepared(
        prepared, reranker=collapsed_scorer, distinct=DistinctResults(_unique, 10)
    )
    kept = multistore_module.execute_prepared(
        prepared, reranker=TableScorer(scores), distinct=DistinctResults(by_origin, 10)
    )
    assert [_note(item) for item in collapsed.hits] == notes
    assert collapsed.distinct == DistinctOutcome(10, 10, 0, 5, 5, True)
    assert len(kept.hits) == 10 and kept.distinct == DistinctOutcome(10, 10, 0, 0, 10, False)
    assert kept.hits[:3] == plain.hits
    # Each copy's first appearance in the ranked pool is the one kept.
    assert collapsed.hits == [
        next(item for item in kept.hits if _note(item) == note) for note in notes
    ]
    assert collapsed_scorer.calls == plain_scorer.calls and len(plain_scorer.calls) == 1
    assert len(plain_scorer.calls[0][1]) == 10
    assert (embedder.call_count, embedder.all_batches, expander.calls) == prepared_calls


@pytest.mark.parametrize("seam", ["single-store", "multi-store"])
@pytest.mark.parametrize("cached", [False, True])
def test_the_key_runs_after_every_snapshot_and_lease_has_closed(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, seam: str, cached: bool
) -> None:
    store = _store(isolated, "closed", [f"g{n:02d}-a" for n in range(1, 5)])
    open_snapshots: list[int] = []
    cache = VectorCache(budget_bytes=64 * 2**20, on_unavailable="fallback") if cached else None

    original_single = search_module._admit_store

    @contextmanager
    def single(*args: Any, **kwargs: Any) -> Iterator[Any]:
        open_snapshots.append(1)
        try:
            with original_single(*args, **kwargs) as admitted:
                yield admitted
        finally:
            open_snapshots.pop()

    original_multi = multistore_module._admit_stores

    def multi(*args: Any, **kwargs: Any) -> Any:
        open_snapshots.append(1)
        kwargs["stack"].callback(open_snapshots.pop)  # runs after every connection closes
        return original_multi(*args, **kwargs)

    monkeypatch.setattr(search_module, "_admit_store", single)
    monkeypatch.setattr(multistore_module, "_admit_stores", multi)
    keyed: list[str] = []

    def closed(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
        assert open_snapshots == []
        assert cache is None or cache.stats().leases == 0
        keyed.append(_note(hit))
        return _unique(hit, evidence)

    run: Callable[[], Any]
    if seam == "single-store":
        prepared = _single(store, limit=2, injected_reranker=True)

        def run() -> Any:
            return execute_prepared_search(
                prepared,
                reranker=TableScorer(),
                vector_cache=cache,
                distinct=DistinctResults(closed, 3),
            )

    else:
        multi_prepared = prepare_store_search(
            query="lantern",
            stores=[store],
            pool=50,
            limit=2,
            embedders={resolve_identity(store): FakeEmbedder()},
            injected_reranker=True,
        )

        def run() -> Any:
            return multistore_module.execute_prepared(
                multi_prepared,
                reranker=TableScorer(),
                vector_cache=cache,
                distinct=DistinctResults(closed, 3),
            )

    result = run()
    assert len(result.hits) == 3 and len(keyed) == 3
    assert (result.vector_cache is not None) == cached


def test_distinct_refuses_malformed_requests_and_propagates_key_failures(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(isolated, "refusals", ["g01-a", "g02-a"])
    prepared = _single(store, limit=2, injected_reranker=True)
    multi = prepare_store_search(
        query="lantern",
        stores=[store],
        embedders={resolve_identity(store): FakeEmbedder()},
        injected_reranker=True,
    )
    for key, limit, message in (
        ("not callable", 1, "distinct key must be callable"),
        (_unique, 0, "distinct limit must be a positive integer"),
        (_unique, True, "distinct limit must be a positive integer"),
        (_unique, 1.5, "distinct limit must be a positive integer"),
    ):
        with pytest.raises(SearchError, match=message):
            DistinctResults(key, limit)  # type: ignore[arg-type]

    work: list[str] = []
    original_single, original_multi = search_module._admit_store, multistore_module._admit_stores
    monkeypatch.setattr(
        search_module,
        "_admit_store",
        lambda *a, **k: (work.append("admit"), original_single(*a, **k))[1],
    )
    monkeypatch.setattr(
        multistore_module,
        "_admit_stores",
        lambda *a, **k: (work.append("admit"), original_multi(*a, **k))[1],
    )
    cache = VectorCache(budget_bytes=64 * 2**20, on_unavailable="fallback")
    original_lease = cache.lease
    monkeypatch.setattr(
        cache, "lease", lambda *a, **k: (work.append("lease"), original_lease(*a, **k))[1]
    )
    scorer = TableScorer()
    for execute in (
        lambda value, **options: execute_prepared_search(
            prepared, reranker=scorer, distinct=value, **options
        ),
        lambda value, **options: multistore_module.execute_prepared(
            multi, reranker=scorer, distinct=value, **options
        ),
    ):
        for options in ({}, {"vector_cache": cache}):
            with pytest.raises(SearchError, match="distinct must be a DistinctResults value"):
                execute((_unique, 1), **options)
            assert work == [] and scorer.calls == []  # refused before any admission or call
        with pytest.raises(SearchError, match="distinct key must return a string or None"):
            execute(DistinctResults(lambda hit, evidence: 7, 1))  # type: ignore[arg-type,return-value]
        assert work and scorer.calls  # a valid request does admit and score
        work.clear()
        scorer.calls.clear()

        class KeyFailure(Exception):
            pass

        def failing(hit: SearchHit | StoreHit, evidence: HitEvidence | None) -> str:
            raise KeyFailure("caller check failed")

        with pytest.raises(KeyFailure, match="caller check failed"):
            execute(DistinctResults(failing, 1))
        # With None the result is the plain one.
        assert execute(None) == execute(None) and execute(None).distinct is None
        work.clear()
        scorer.calls.clear()
