"""Opt-in exact in-memory vector cache for long-lived host processes (Phase 35).

A host that constructs a :class:`VectorCache` and passes it as ``vector_cache`` to
:func:`palace.search.execute_prepared_search` or :func:`palace.multistore.execute_prepared`
serves the vector leg of each store that has the change feed enabled
(``palace index vector-feed enable``) from a process-local copy of that store's vectors.
Nothing else imports this module: a search without a cache runs exactly the released
code.

Correctness contract:

- **Same answer as the released search.** Candidates are chosen by an approximate
  float32 distance whose error is bounded, and rescored with sqlite-vec's own
  ``vec_distance_l2`` on the query's connection. The cut is used only when it is
  certified: the ``pool``-th rescored distance is below every unrescored row's lower
  bound. Otherwise more rows are rescored, up to every visible row. Returned distances
  are sqlite-vec's. When parity cannot be certified (equal distances among the returned
  rows or at the cut, a filter that admits a chunk without a vector, a filtered row a
  concurrent compaction moved, a pool above sqlite-vec's KNN limit, a non-finite query,
  or a component outside the range the error bound is proved for), that leg runs the
  released SQL on the same connection and is reported as ``oracle``.
- **Pinned views.** :meth:`VectorCache.lease` runs the authorization hook for every
  store, then pins each store's accelerator before the caller admits any store. After
  admission the first vector leg reads its snapshot's feed position, applies the feed
  up to it, and scans only rows born at or before it and not yet dead at it. Every leg,
  rerank and evidence read of the execution uses the same pinned view, and the cached
  single-store path keeps its SQLite snapshot open through the rerank. Compaction never
  drops a row a live lease could still see, and an evicted store's memory is kept until
  its last lease ends.
- **Integrity is never bypassed.** A diverged feed, malformed state, a dimension
  mismatch or an unreconcilable load or refresh fails the query under either setting
  and evicts the store; a leg answered by fallback first checks the feed state on the
  connection that answers it. Only the closed operational class (``feed-disabled``,
  ``budget``, ``queue-full``, ``snapshot``) honours ``on_unavailable``: ``"refuse"``
  raises :class:`VectorCacheUnavailable`; ``"fallback"`` answers from the released SQL
  and reports it.
- **Bounded memory.** One ledger accounts every block (live, retired while leased, or
  pooled), each in-flight load and every query's, refresh's, load's and compaction's
  working memory, reserved before it is allocated, and refuses a reservation beyond
  ``budget_bytes`` after pooled blocks of other widths and unleased stores have been
  released. Ordinary updates append to free slots; nothing copies an index; at most one
  compaction replacement per store is outstanding. Pooled blocks are zero-filled. Byte
  figures are the cache's own accounting, an estimate distinct from the operating
  system's view of the process.
"""

from __future__ import annotations

import math
import resource
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from palace.index._errors import IndexError
from palace.index.config import (
    VECTOR_CACHE_BLOCK_ROWS,
    VECTOR_CACHE_COMPACT_DEAD_FRACTION,
    chunks_db_path,
)
from palace.index.s3vectors import VectorMatch
from palace.index.schema import declared_vector_dim
from palace.index.vector_feed import FEED_TABLE, VectorFeedState, read_vector_feed_state
from palace.metadata import MetadataFilter, matching_chunks
from palace.search import SQLITE_VEC_MAX_K, _admit_store, _open_chunks_db_ro

__all__ = [
    "QUALIFIED_SQLITE_VEC_VERSION",
    "StoreCacheStats",
    "VectorCache",
    "VectorCacheLease",
    "VectorCacheOutcome",
    "VectorCacheStats",
    "VectorCacheUnavailable",
    "process_peak_rss_bytes",
]

QUALIFIED_SQLITE_VEC_VERSION = "v0.1.9"
"""The sqlite-vec release whose storage layout the fast loader was written and tested for."""

OnUnavailable = Literal["refuse", "fallback"]
UnavailableReason = Literal["feed-disabled", "budget", "queue-full", "snapshot"]

_LIVE = int(np.iinfo(np.int64).max)
_UNIT_ROUNDOFF = 2.0**-24
# Every product, square and sum the bound covers stays finite and its inputs are normal
# or zero when each nonzero component lies in this range; a product or square that
# underflows (gradually, or flushed to zero) is then wrong by less than _TINY.
_RANGE_MIN = 2.0**-126
_RANGE_MAX = 2.0**40
_TINY = 2.0**-126
_ID_ROW_BYTES = 160  # estimate per held row: id string, its slot reference and map entry
_SCORE_ROW_BYTES = 128  # scorer arrays per visible row, counting the concatenated copies
_COPY_ROWS = 256  # rows copied or rescored at a time
_CHECK_ROWS = 16  # rows whose finiteness, range and norms a load checks at a time
_FAST_SAMPLES = 16
_FAST_LOCATIONS_SQL = (
    "SELECT id, chunk_id, chunk_offset FROM chunks_vec_rowids ORDER BY chunk_id, chunk_offset"
)


def _initial_margin(pool: int) -> int:
    """Rows rescored beyond ``pool`` on the first try; doubled until the cut is certified."""
    return max(pool, 16)


def _gamma(n: int) -> float:
    return n * _UNIT_ROUNDOFF / (1.0 - n * _UNIT_ROUNDOFF)


def _out_of_range(rows: np.ndarray) -> np.ndarray:
    """Per row: whether a nonzero component lies outside the proved range."""
    magnitude = np.abs(rows)
    outside: np.ndarray = (
        (magnitude != 0) & ((magnitude < _RANGE_MIN) | (magnitude > _RANGE_MAX))
    ).any(axis=-1)
    return outside


def _key(store: Path) -> Path:
    """One canonical identity per store directory, whatever spelling the caller used."""
    return Path(store).expanduser().resolve()


class VectorCacheUnavailable(IndexError):
    """The cache cannot serve a store for an operational reason and the cache refuses.

    ``reason`` is one of ``feed-disabled``, ``budget``, ``queue-full`` or ``snapshot``.
    """

    def __init__(self, store: Path, reason: str) -> None:
        super().__init__(f"vector cache unavailable for store {store}: {reason}")
        self.store = store
        self.reason = reason


class _Unavailable(Exception):
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason: UnavailableReason = reason


class _FastLoadRejected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class VectorCacheOutcome:
    """How one store's vector legs were served in one execution.

    ``cached_legs`` were answered from memory, ``oracle_legs`` by the released SQL because
    parity could not be certified, and ``fallback_legs`` by the released SQL because the
    cache was unavailable (``reasons`` says why). ``bypassed`` stores had no sqlite-vec
    vector leg to serve (keyword-only search, an S3 Vectors store, or no database).
    ``snapshot_seq`` is the feed position the legs read, when the cache prepared a view.
    ``store`` is the canonical (resolved) store directory.
    """

    store: Path
    bypassed: bool
    cached_legs: int
    oracle_legs: int
    fallback_legs: int
    reasons: tuple[str, ...]
    snapshot_seq: int | None


@dataclass(frozen=True, slots=True)
class StoreCacheStats:
    """One resident store: O(1) figures, keyed by lineage epoch and feed position.

    ``applied_seq``, ``live_rows`` and ``dead_rows`` are published together, so they
    always describe one committed state.
    """

    store: Path
    epoch: str
    base_seq: int
    applied_seq: int
    live_rows: int
    dead_rows: int
    loader_route: str
    load_seconds: float
    owned_bytes: int
    leases: int


@dataclass(frozen=True, slots=True)
class VectorCacheStats:
    """Cache-wide figures from one ledger snapshot.

    ``owned_bytes`` is the sum of ``live_bytes`` (blocks of resident stores),
    ``retired_bytes`` (blocks kept only for live leases), ``pooled_bytes`` (zeroed blocks
    kept for reuse), ``load_bytes`` (blocks held by in-flight loads) and
    ``scratch_bytes`` (working memory of queries, refreshes, loads and compactions).
    ``headroom_bytes`` is what remains under ``budget_bytes``. Counters are cumulative
    and keyed by event and reason.
    """

    budget_bytes: int
    owned_bytes: int
    headroom_bytes: int
    live_bytes: int
    retired_bytes: int
    pooled_bytes: int
    load_bytes: int
    scratch_bytes: int
    leases: int
    counters: dict[str, int]
    stores: tuple[StoreCacheStats, ...]


def process_peak_rss_bytes() -> int | None:
    """The process's lifetime peak resident set size, as the operating system reports it."""
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


class _Block:
    """Fixed-capacity rows with insert (``born``) and delete (``dead``) feed positions.

    Rows below ``used`` never change while any other lease can read them, except their
    ``dead`` stamp, which moves once from live to a position later than any reader that
    could have seen the row as dead. ``wild`` marks a row with a component outside the
    range the distance bound is proved for.
    """

    __slots__ = ("born", "data", "dead", "dead_rows", "dim", "ids", "norm2", "used", "wild")

    def __init__(self, rows: int, dim: int) -> None:
        self.dim = dim
        self.data = np.zeros((rows, dim), np.float32)
        self.norm2 = np.zeros(rows, np.float64)
        self.born = np.zeros(rows, np.int64)
        self.dead = np.full(rows, _LIVE, np.int64)
        self.wild = np.zeros(rows, np.bool_)
        self.ids: list[str | None] = [None] * rows
        self.used = 0
        self.dead_rows = 0

    def wipe(self) -> None:
        self.data.fill(0.0)
        self.norm2.fill(0.0)
        self.born.fill(0)
        self.dead.fill(_LIVE)
        self.wild.fill(False)
        self.ids = [None] * len(self.ids)
        self.used = 0
        self.dead_rows = 0

    @property
    def capacity(self) -> int:
        return len(self.ids)

    def put(self, slot: int, chunk_id: str, vector: np.ndarray, born: int) -> None:
        self.data[slot] = vector
        self.norm2[slot] = float(np.dot(vector, vector))
        self.wild[slot] = bool(_out_of_range(vector))
        self.ids[slot] = chunk_id
        self.born[slot] = born
        self.dead[slot] = _LIVE

    def append(self, chunk_id: str, vector: np.ndarray, born: int) -> int:
        slot = self.used
        self.put(slot, chunk_id, vector, born)
        self.used = slot + 1
        return slot


_View = tuple[tuple[_Block, int], ...]


class _Accelerator:
    """One store's resident vectors for one lineage epoch."""

    def __init__(
        self,
        *,
        store: Path,
        epoch: str,
        dim: int,
        base_seq: int,
        route: str,
        load_seconds: float,
        blocks: list[_Block],
    ) -> None:
        self.store = store
        self.epoch = epoch
        self.dim = dim
        self.base_seq = base_seq
        self.applied = base_seq
        self.exact_from = base_seq
        self.route = route
        self.load_seconds = load_seconds
        self.blocks = blocks
        self.where: dict[str, list[tuple[_Block, int]]] = {}
        self.live_rows = 0
        self.dead_rows = 0
        for block in blocks:
            for slot in range(block.used):
                chunk_id = block.ids[slot]
                assert chunk_id is not None
                self.where.setdefault(chunk_id, []).append((block, slot))
                self.live_rows += 1
        self.refresh_lock = threading.Lock()
        self.leases = 0
        self.horizons: Counter[int] = Counter()
        self.view_gen = 0
        self.view_refs: Counter[int] = Counter()
        self.retired: list[tuple[int, _Block]] = []
        self.evicted = False
        self.stale: str | None = None

    def live_slot(self, chunk_id: str) -> tuple[_Block, int] | None:
        for block, slot in self.where.get(chunk_id, ()):
            if block.dead[slot] == _LIVE:
                return block, slot
        return None


class _Loading:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.acc: _Accelerator | None = None


@dataclass
class _Entry:
    generation: int = 0
    acc: _Accelerator | None = None
    loading: _Loading | None = None
    last_used: float = 0.0


class _StoreHandle:
    """One store's part of one lease: the pinned accelerator and the legs it served."""

    def __init__(
        self,
        cache: VectorCache,
        store: Path,
        *,
        acc: _Accelerator | None,
        horizon: int | None,
        reason: UnavailableReason | None,
        bypassed: bool,
    ) -> None:
        self.cache = cache
        self.store = store
        self.acc = acc
        self.horizon = horizon
        self.reason = reason
        self.bypassed = bypassed
        self.checked = False
        self.live_chunks = 0
        self.view: _View | None = None
        self.view_gen: int | None = None
        self.snapshot_seq: int | None = None
        self.cached_legs = 0
        self.oracle_legs = 0
        self.fallback_legs = 0
        self.reasons: set[str] = set()

    def outcome(self) -> VectorCacheOutcome:
        return VectorCacheOutcome(
            store=self.store,
            bypassed=self.bypassed,
            cached_legs=self.cached_legs,
            oracle_legs=self.oracle_legs,
            fallback_legs=self.fallback_legs,
            reasons=tuple(sorted(self.reasons)),
            snapshot_seq=self.snapshot_seq,
        )

    def search(
        self,
        conn: sqlite3.Connection,
        embedding: Sequence[float],
        pool: int,
        filters: Sequence[MetadataFilter],
    ) -> list[VectorMatch] | None:
        """Serve one vector leg, or return ``None`` for the released SQL to answer it."""
        if self.bypassed:
            raise IndexError("vector cache leg requested for a bypassed store")
        if self.reason is not None:
            self._check_fallback(conn)
        elif self.view is None:
            self._prepare(conn)
        if self.reason is not None:
            self.fallback_legs += 1
            self.reasons.add(self.reason)
            self.cache._count(f"fallbacks.{self.reason}")
            return None
        assert self.acc is not None and self.view is not None and self.snapshot_seq is not None
        query = np.asarray(embedding, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != self.acc.dim:
            raise IndexError(
                f"query vector has {query.size} dimensions; the store's vectors have {self.acc.dim}"
            )
        if pool > SQLITE_VEC_MAX_K:
            self._oracle("knn-limit")
            return None
        if not np.isfinite(query).all():
            self._oracle("non-finite-query")
            return None
        if bool(_out_of_range(query)):
            self._oracle("numeric-range")
            return None
        try:
            result = self._scan(conn, query, pool, filters)
        except _Unavailable as unavailable:
            if self.cache.on_unavailable == "refuse":
                self.cache._count(f"refusals.{unavailable.reason}")
                raise VectorCacheUnavailable(self.store, unavailable.reason) from None
            self.fallback_legs += 1
            self.reasons.add(unavailable.reason)
            self.cache._count(f"fallbacks.{unavailable.reason}")
            return None
        except BaseException:
            self.cache._evict(self.store, "integrity")
            raise
        if isinstance(result, str):
            self._oracle(result)
            return None
        self.cached_legs += 1
        return result

    def _oracle(self, reason: str) -> None:
        self.oracle_legs += 1
        self.reasons.add(reason)
        self.cache._count(f"oracle.{reason}")

    def _check_fallback(self, conn: sqlite3.Connection) -> None:
        """Before an operational fallback answers, refuse a diverged or malformed feed.

        The check reads the admitted snapshot once per lease; a store whose feed is not
        enabled has nothing to check.
        """
        if self.checked:
            return
        try:
            state = read_vector_feed_state(conn)
            if state is not None:
                state.check()
        except BaseException:
            self.cache._evict(self.store, "integrity")
            raise
        self.checked = True

    def _prepare(self, conn: sqlite3.Connection) -> None:
        acc = self.acc
        assert acc is not None
        try:
            try:
                state = read_vector_feed_state(conn)
                if state is None:
                    self.cache._evict(self.store, "feed-disabled")
                    raise _Unavailable("feed-disabled")
                state.check()
                self.checked = True
                seq = state.last_seq
                self.live_chunks = state.live_chunks
                if state.epoch != acc.epoch:
                    acc.stale = "lineage"
                    raise _Unavailable("snapshot")
                if seq < acc.exact_from or (self.horizon is not None and seq < self.horizon):
                    raise _Unavailable("snapshot")
                # From here this lease needs nothing older than its own snapshot.
                self.horizon = self.cache._move_horizon(acc, self.horizon, seq)
                self.cache._refresh(acc, conn, state)
                if seq < acc.exact_from:
                    raise _Unavailable("snapshot")
            except _Unavailable:
                raise
            except BaseException:
                self.cache._evict(self.store, "integrity")
                raise
        except _Unavailable as unavailable:
            if self.cache.on_unavailable == "refuse":
                self.cache._count(f"refusals.{unavailable.reason}")
                raise VectorCacheUnavailable(self.store, unavailable.reason) from None
            self.reason = unavailable.reason
            return
        self.cache._compact_if_due(acc)
        captured = self.cache._capture_view(acc)
        if captured is None:
            # Evicted after this lease pinned it: no view represents the snapshot.
            if self.cache.on_unavailable == "refuse":
                self.cache._count("refusals.snapshot")
                raise VectorCacheUnavailable(self.store, "snapshot")
            self.reason = "snapshot"
            return
        self.snapshot_seq = seq
        self.view, self.view_gen = captured

    def _scan(
        self,
        conn: sqlite3.Connection,
        query: np.ndarray,
        pool: int,
        filters: Sequence[MetadataFilter],
    ) -> list[VectorMatch] | str:
        acc, view, seq = self.acc, self.view, self.snapshot_seq
        assert acc is not None and view is not None and seq is not None
        held = sum(used for _block, used in view)
        widest = max((used for _block, used in view), default=0)
        # Per visible row: scores, bounds and positions with their concatenated copies;
        # one rescoring batch of vector bytes; with a filter, the matched id set and one
        # block's gathered rows.
        scratch = held * _SCORE_ROW_BYTES + min(_COPY_ROWS, held) * acc.dim * 4
        if filters:
            scratch += (self.live_chunks + held) * _ID_ROW_BYTES + widest * acc.dim * 4
        if not self.cache._reserve_scratch(scratch, exclude=self.store):
            raise _Unavailable("budget")
        try:
            allowed: dict[int, list[int]] | None = None
            if filters:
                sql, parameters = matching_chunks(filters)
                index = {id(block): position for position, (block, _used) in enumerate(view)}
                allowed = {}
                seen: set[str] = set()
                # The id map is read under the refresh lock, so no refresh or compaction
                # edits it meanwhile; a row compaction relocated after this view was taken
                # is not in the view, and the released SQL answers instead.
                with acc.refresh_lock:
                    for (raw_id,) in conn.execute(sql, parameters):
                        chunk_id = str(raw_id)
                        if chunk_id in seen:
                            continue
                        seen.add(chunk_id)
                        located = _visible_slot(acc, chunk_id, seq)
                        if located is None:
                            return "missing-vector"
                        block, slot = located
                        position = index.get(id(block))
                        if position is None or slot >= view[position][1]:
                            return "relocated"
                        allowed.setdefault(position, []).append(slot)
                del seen
            return _exact_top(conn, view, seq, query, pool, allowed, self.cache)
        finally:
            self.cache._release_scratch(scratch)


def _visible_slot(acc: _Accelerator, chunk_id: str, seq: int) -> tuple[_Block, int] | None:
    for block, slot in acc.where.get(chunk_id, ()):
        if block.born[slot] <= seq < block.dead[slot]:
            return block, slot
    return None


def _exact_top(
    conn: sqlite3.Connection,
    view: _View,
    seq: int,
    query: np.ndarray,
    pool: int,
    allowed: dict[int, list[int]] | None,
    cache: VectorCache,
) -> list[VectorMatch] | str:
    """The released top ``pool`` by sqlite-vec distance, or an oracle reason.

    Within the proved range (every nonzero component of the query and of each visible
    row in ``[2**-126, 2**40]``), for dimension ``d``: the float32 approximation
    ``|x|^2 - 2 x.q + |q|^2`` differs from the exact squared distance by at most
    ``gamma_{d+2} (|x| + |q|)^2 / (1 - gamma_d) + 8 d 2**-126``, in any summation order
    and whether underflow is gradual or flushed to zero; sqlite-vec's float32 distance
    squared is at least ``(1 - gamma_{d+4})`` times the exact square minus ``4 d 2**-126``.
    """
    dim = query.shape[0]
    query_norm2 = float(np.dot(query, query))
    approx_gamma = _gamma(dim + 2) / (1.0 - _gamma(dim))
    approx_tiny = 8 * dim * _TINY
    query_norm = math.sqrt(query_norm2)
    approx_parts: list[np.ndarray] = []
    bound_parts: list[np.ndarray] = []
    block_parts: list[np.ndarray] = []
    slot_parts: list[np.ndarray] = []
    for position, (block, used) in enumerate(view):
        if used == 0:
            continue
        if allowed is None:
            slots = np.flatnonzero((block.born[:used] <= seq) & (block.dead[:used] > seq))
            if slots.size == 0:
                continue
            dots = (block.data[:used] @ query)[slots]
        else:
            chosen = allowed.get(position)
            if not chosen:
                continue
            slots = np.asarray(chosen, dtype=np.int64)
            dots = block.data[slots] @ query
        if block.wild[slots].any():
            return "numeric-range"
        norm2 = block.norm2[slots]
        approx_parts.append(norm2 - 2.0 * dots.astype(np.float64) + query_norm2)
        bound_parts.append(approx_gamma * (np.sqrt(norm2) + query_norm) ** 2 + approx_tiny)
        block_parts.append(np.full(slots.size, position, dtype=np.int64))
        slot_parts.append(slots)
    if not approx_parts:
        return []
    approx = np.concatenate(approx_parts)
    bound = np.concatenate(bound_parts)
    del approx_parts, bound_parts
    blocks = np.concatenate(block_parts)
    slots = np.concatenate(slot_parts)
    del block_parts, slot_parts
    visible = approx.size
    lower = (1.0 - _gamma(dim + 4)) * (approx - bound) - 4 * dim * _TINY
    del bound
    query_blob = query.tobytes()
    scored: dict[int, float] = {}
    margin = _initial_margin(pool)
    while True:
        count = min(visible, pool + margin)
        if count < visible:
            candidates = np.argpartition(approx, count - 1)[:count]
        else:
            candidates = np.arange(visible)
        pending = [int(row) for row in candidates if int(row) not in scored]
        for start in range(0, len(pending), _COPY_ROWS):
            batch = pending[start : start + _COPY_ROWS]
            expressions = ", ".join(
                f"vec_distance_l2(?1, ?{offset + 2})" for offset in range(len(batch))
            )
            parameters = [query_blob] + [
                view[int(blocks[row])][0].data[int(slots[row])].tobytes() for row in batch
            ]
            values = conn.execute(f"SELECT {expressions}", parameters).fetchone()
            for row, value in zip(batch, values, strict=True):
                scored[row] = float(value)
        ordered = sorted((scored[int(row)], int(row)) for row in candidates)
        returned = ordered[: min(pool, visible)]
        distances = [distance for distance, _row in returned]
        if any(left == right for left, right in zip(distances, distances[1:], strict=False)):
            return "tie"
        if len(ordered) > len(returned) and ordered[len(returned)][0] == distances[-1]:
            return "tie"
        if count == visible:
            break
        outside = np.ones(visible, dtype=bool)
        outside[candidates] = False
        threshold = distances[-1] * distances[-1]
        if threshold < float(lower[outside].min()):
            break
        margin *= 2
        cache._count("cut.widened")
    matches = []
    for distance, row in returned:
        chunk_id = view[int(blocks[row])][0].ids[int(slots[row])]
        assert chunk_id is not None
        matches.append(VectorMatch(chunk_id=chunk_id, distance=distance))
    return matches


class VectorCacheLease:
    """The pinned per-store views of one execution; release it on every exit path."""

    def __init__(self, cache: VectorCache, handles: dict[Path, _StoreHandle]) -> None:
        self._cache = cache
        self._handles = handles

    def handle(self, store: Path) -> _StoreHandle | None:
        """The store's cache handle, or ``None`` when its vector leg is not served here."""
        handle = self._handles.get(_key(store))
        if handle is None or handle.bypassed:
            return None
        return handle

    def outcomes(self) -> tuple[VectorCacheOutcome, ...]:
        return tuple(handle.outcome() for handle in self._handles.values())


class VectorCache:
    """A process-wide, budgeted, exact in-memory cache of opted-in stores' vectors.

    ``on_unavailable`` has no default: ``"refuse"`` raises :class:`VectorCacheUnavailable`
    for an operational reason, ``"fallback"`` answers from the released SQL and reports
    it in the result. ``authorize(store)`` receives the canonical store directory for
    every store of every execution before admission and must return true; a refusal or
    error fails the execution and evicts the store. Every store is keyed by its resolved
    directory, so any spelling of one store names the same residency.
    """

    def __init__(
        self,
        *,
        budget_bytes: int,
        on_unavailable: OnUnavailable,
        authorize: Callable[[Path], bool] | None = None,
        max_concurrent_loads: int = 1,
        max_waiting_loads: int = 8,
        block_rows: int = VECTOR_CACHE_BLOCK_ROWS,
    ) -> None:
        if on_unavailable not in ("refuse", "fallback"):
            raise ValueError("on_unavailable must be 'refuse' or 'fallback'")
        if not isinstance(budget_bytes, int) or budget_bytes <= 0:
            raise ValueError("budget_bytes must be a positive integer")
        if max_concurrent_loads < 1 or max_waiting_loads < 1 or block_rows < 1:
            raise ValueError("load limits and block_rows must be positive")
        self.budget_bytes = budget_bytes
        self.on_unavailable: OnUnavailable = on_unavailable
        self.authorize = authorize
        self.max_waiting_loads = max_waiting_loads
        self.block_rows = block_rows
        self._lock = threading.Lock()
        self._load_slots = threading.Semaphore(max_concurrent_loads)
        self._entries: dict[Path, _Entry] = {}
        self._pool: dict[int, list[_Block]] = {}
        self._bytes: dict[str, int] = dict.fromkeys(
            ("live", "retired", "pooled", "load", "scratch"), 0
        )
        self._waiting = 0
        self._leases = 0
        self._counters: Counter[str] = Counter()

    # -- public surface -------------------------------------------------------------

    @contextmanager
    def lease(
        self, stores: Sequence[Path], *, bypass: Collection[Path] = ()
    ) -> Iterator[VectorCacheLease]:
        """Authorize every store, then pin each one's view, before the caller admits any.

        Stores in ``bypass`` (no sqlite-vec vector leg in this execution) are authorized
        and reported but not loaded. The lease must enclose retrieval, rerank and
        evidence; it releases every pin on exit.
        """
        roots = list(dict.fromkeys(_key(store) for store in stores))
        skipped = {_key(store) for store in bypass}
        for store in roots:
            self._authorize(store)
        handles: dict[Path, _StoreHandle] = {}
        try:
            for store in roots:
                if store in skipped or not chunks_db_path(store).is_file():
                    handles[store] = _StoreHandle(
                        self, store, acc=None, horizon=None, reason=None, bypassed=True
                    )
                    continue
                try:
                    acc, horizon = self._acquire(store)
                except _Unavailable as unavailable:
                    if self.on_unavailable == "refuse":
                        self._count(f"refusals.{unavailable.reason}")
                        raise VectorCacheUnavailable(store, unavailable.reason) from None
                    handles[store] = _StoreHandle(
                        self,
                        store,
                        acc=None,
                        horizon=None,
                        reason=unavailable.reason,
                        bypassed=False,
                    )
                    continue
                handles[store] = _StoreHandle(
                    self, store, acc=acc, horizon=horizon, reason=None, bypassed=False
                )
            with self._lock:
                self._leases += 1
            try:
                yield VectorCacheLease(self, handles)
            finally:
                with self._lock:
                    self._leases -= 1
        finally:
            self._release(handles.values())

    def warm(self, store: Path) -> bool:
        """Authorize, admit and load one store ahead of its first query.

        Returns whether the store is now resident; a store without a sqlite-vec vector
        leg is not loaded. Operational unavailability follows ``on_unavailable``.
        """
        store = _key(store)
        self._authorize(store)
        with _admit_store(store=store, mode="vector") as (_conn, _binding, backend):
            if backend is not None:
                return False
        try:
            acc, horizon = self._acquire(store)
        except _Unavailable as unavailable:
            if self.on_unavailable == "refuse":
                self._count(f"refusals.{unavailable.reason}")
                raise VectorCacheUnavailable(store, unavailable.reason) from None
            self._count(f"fallbacks.{unavailable.reason}")
            return False
        self._release(
            [_StoreHandle(self, store, acc=acc, horizon=horizon, reason=None, bypassed=False)]
        )
        return True

    def evict(self, store: Path) -> bool:
        """Unpublish one store now; its memory returns when its last lease ends."""
        return self._evict(_key(store), "operator")

    def stats(self) -> VectorCacheStats:
        with self._lock:
            stores = []
            for store, entry in self._entries.items():
                acc = entry.acc
                if acc is None:
                    continue
                stores.append(
                    StoreCacheStats(
                        store=store,
                        epoch=acc.epoch,
                        base_seq=acc.base_seq,
                        applied_seq=acc.applied,
                        live_rows=acc.live_rows,
                        dead_rows=acc.dead_rows,
                        loader_route=acc.route,
                        load_seconds=acc.load_seconds,
                        owned_bytes=(len(acc.blocks) + len(acc.retired))
                        * self._block_bytes(acc.dim),
                        leases=acc.leases,
                    )
                )
            owned = sum(self._bytes.values())
            return VectorCacheStats(
                budget_bytes=self.budget_bytes,
                owned_bytes=owned,
                headroom_bytes=self.budget_bytes - owned,
                live_bytes=self._bytes["live"],
                retired_bytes=self._bytes["retired"],
                pooled_bytes=self._bytes["pooled"],
                load_bytes=self._bytes["load"],
                scratch_bytes=self._bytes["scratch"],
                leases=self._leases,
                counters=dict(self._counters),
                stores=tuple(stores),
            )

    # -- ledger ---------------------------------------------------------------------

    def _count(self, key: str) -> None:
        with self._lock:
            self._counters[key] += 1

    def _block_bytes(self, dim: int) -> int:
        return self.block_rows * (dim * 4 + 8 + 16 + 1 + _ID_ROW_BYTES)

    def _headroom(self) -> int:
        return self.budget_bytes - sum(self._bytes.values())

    def _make_room_locked(self, keep_dim: int | None, exclude: Path | None) -> list[_Block] | None:
        """Release one pooled block of another width, else evict the least recently used
        unleased store. Returns the blocks to recycle outside the lock, or ``None`` when
        nothing could be released.
        """
        for dim, pooled in self._pool.items():
            if dim != keep_dim and pooled:
                pooled.pop()
                self._bytes["pooled"] -= self._block_bytes(dim)
                return []
        candidates = [
            (entry.last_used, str(store), store)
            for store, entry in self._entries.items()
            if entry.acc is not None and entry.acc.leases == 0 and store != exclude
        ]
        if not candidates:
            return None
        victim = min(candidates)[2]
        return self._unpublish_locked(self._entries[victim], "pressure")

    def _take_blocks(
        self, count: int, dim: int, category: str, exclude: Path
    ) -> list[_Block] | None:
        taken: list[_Block] = []
        size = self._block_bytes(dim)
        while True:
            with self._lock:
                pooled = self._pool.setdefault(dim, [])
                while pooled and len(taken) < count:
                    taken.append(pooled.pop())
                    self._bytes["pooled"] -= size
                    self._bytes[category] += size
                rest = count - len(taken)
                if rest * size <= self._headroom():
                    self._bytes[category] += rest * size
                    break
                freed = self._make_room_locked(dim, exclude)
                if freed is None:
                    for block in taken:
                        self._bytes[category] -= size
                        self._bytes["pooled"] += size
                        pooled.append(block)
                    self._counters["reservation_refusals"] += 1
                    return None
            self._recycle(freed)
        taken.extend(_Block(self.block_rows, dim) for _ in range(rest))
        return taken

    def _reserve_scratch(self, nbytes: int, *, exclude: Path) -> bool:
        while True:
            with self._lock:
                if nbytes <= self._headroom():
                    self._bytes["scratch"] += nbytes
                    return True
                freed = self._make_room_locked(None, exclude)
                if freed is None:
                    self._counters["reservation_refusals"] += 1
                    return False
            self._recycle(freed)

    def _release_scratch(self, nbytes: int) -> None:
        with self._lock:
            self._bytes["scratch"] -= nbytes

    def _recycle(self, blocks: list[_Block]) -> None:
        """Zero blocks outside the lock, then pool them; they count as retired until then."""
        for block in blocks:
            block.wipe()
        if not blocks:
            return
        with self._lock:
            for block in blocks:
                size = self._block_bytes(block.dim)
                self._bytes["retired"] -= size
                self._bytes["pooled"] += size
                self._pool.setdefault(block.dim, []).append(block)

    def _give_back(self, blocks: list[_Block], category: str) -> None:
        with self._lock:
            for block in blocks:
                size = self._block_bytes(block.dim)
                self._bytes[category] -= size
                self._bytes["retired"] += size
        self._recycle(blocks)

    # -- residency ------------------------------------------------------------------

    def _authorize(self, store: Path) -> None:
        if self.authorize is None:
            return
        try:
            allowed = self.authorize(store)
        except BaseException:
            self._evict(store, "authorization")
            raise
        if not allowed:
            self._evict(store, "authorization")
            raise IndexError(f"vector cache authorization refused for store {store}")

    def _unpublish_locked(self, entry: _Entry, reason: str) -> list[_Block]:
        """Unpublish under the lock and fence any outstanding load or waiter.

        The accelerator's blocks count as retired from now on; they are freed (returned
        to recycle outside the lock) only once no lease holds the accelerator, because
        a lease may still be refreshing, compacting or scanning them.
        """
        entry.generation += 1
        acc = entry.acc
        if acc is None:
            return []
        entry.acc = None
        acc.evicted = True
        self._counters[f"evictions.{reason}"] += 1
        moved = len(acc.blocks) * self._block_bytes(acc.dim)
        self._bytes["live"] -= moved
        self._bytes["retired"] += moved
        return self._free_retired_locked(acc)

    def _evict(self, store: Path, reason: str) -> bool:
        with self._lock:
            entry = self._entries.get(store)
            if entry is None:
                return False
            resident = entry.acc is not None
            freed = self._unpublish_locked(entry, reason)
        self._recycle(freed)
        return resident

    def _acquire(self, store: Path) -> tuple[_Accelerator, int]:
        """Pin the store's resident accelerator, loading it once if needed.

        Concurrent first executions share one load (single flight); loads run at most
        ``max_concurrent_loads`` at a time, and at most ``max_waiting_loads`` executions
        wait for one, the loader included until it starts. Every attempt is bound to the
        store's generation when it began: an eviction or revocation meanwhile fences it,
        so neither the loader nor a waiter publishes or pins anything, and a later
        execution starts again with its own authorization.
        """
        freed: list[_Block] = []
        pinned: tuple[_Accelerator, int] | None = None
        loading: _Loading | None = None
        leader = False
        generation = 0
        with self._lock:
            entry = self._entries.setdefault(store, _Entry())
            if entry.acc is not None and entry.acc.stale is not None:
                self._counters[f"reloads.{entry.acc.stale}"] += 1
                freed = self._unpublish_locked(entry, "reload")
            if entry.acc is not None:
                pinned = self._pin_locked(entry, entry.acc)
            elif self._waiting < self.max_waiting_loads:
                self._waiting += 1
                generation = entry.generation
                loading = entry.loading
                if loading is None:
                    loading = entry.loading = _Loading()
                    leader = True
        self._recycle(freed)
        if pinned is not None:
            return pinned
        if loading is None:
            raise _Unavailable("queue-full")
        if not leader:
            loading.done.wait()
            with self._lock:
                self._waiting -= 1
                error = loading.error
                acc = entry.acc
                if (
                    error is None
                    and entry.generation == generation
                    and acc is not None
                    and acc is loading.acc
                ):
                    return self._pin_locked(entry, acc)
            if isinstance(error, _Unavailable):
                raise _Unavailable(error.reason)
            if error is not None:
                raise IndexError(f"vector cache load failed for store {store}: {error}")
            raise _Unavailable("snapshot")
        self._load_slots.acquire()
        with self._lock:
            self._waiting -= 1
        try:
            acc = self._load(store)
        except BaseException as exc:
            loading.error = exc
            with self._lock:
                if entry.loading is loading:
                    entry.loading = None
            loading.done.set()
            raise
        finally:
            self._load_slots.release()
        discarded: list[_Block] = []
        with self._lock:
            if entry.loading is loading:
                entry.loading = None
            size = self._block_bytes(acc.dim) * len(acc.blocks)
            self._bytes["load"] -= size
            if entry.generation == generation and entry.acc is None:
                self._bytes["live"] += size
                entry.acc = acc
                loading.acc = acc
                self._counters["loads"] += 1
                pinned = self._pin_locked(entry, acc)
            else:
                self._bytes["retired"] += size
                self._counters["discarded_loads"] += 1
                loading.error = _Unavailable("snapshot")
                discarded = acc.blocks
                acc.blocks = []
        loading.done.set()
        self._recycle(discarded)
        if pinned is None:
            # Evicted or revoked while loading: the load is never published.
            raise _Unavailable("snapshot")
        return pinned

    def _pin_locked(self, entry: _Entry, acc: _Accelerator) -> tuple[_Accelerator, int]:
        entry.last_used = time.monotonic()
        acc.leases += 1
        horizon = acc.applied
        acc.horizons[horizon] += 1
        return acc, horizon

    def _move_horizon(self, acc: _Accelerator, horizon: int | None, seq: int) -> int:
        """Replace a lease's registered horizon with its snapshot position."""
        with self._lock:
            if horizon is not None:
                acc.horizons[horizon] -= 1
                if acc.horizons[horizon] <= 0:
                    del acc.horizons[horizon]
            acc.horizons[seq] += 1
            return seq

    def _capture_view(self, acc: _Accelerator) -> tuple[_View, int] | None:
        """Pin the current block list for this lease, or ``None`` once evicted."""
        with self._lock:
            if acc.evicted:
                return None
            acc.view_refs[acc.view_gen] += 1
            view = tuple((block, block.used) for block in acc.blocks)
            return view, acc.view_gen

    def _compact_if_due(self, acc: _Accelerator) -> None:
        """Compact one due block unless a refresh is running; never waits for one."""
        if acc.refresh_lock.acquire(blocking=False):
            try:
                self._compact(acc)
            finally:
                acc.refresh_lock.release()

    def _release(self, handles: Collection[_StoreHandle]) -> None:
        freed: list[_Block] = []
        with self._lock:
            for handle in handles:
                acc = handle.acc
                if acc is None:
                    continue
                handle.acc = None
                if handle.view_gen is not None:
                    acc.view_refs[handle.view_gen] -= 1
                    if acc.view_refs[handle.view_gen] <= 0:
                        del acc.view_refs[handle.view_gen]
                if handle.horizon is not None:
                    acc.horizons[handle.horizon] -= 1
                    if acc.horizons[handle.horizon] <= 0:
                        del acc.horizons[handle.horizon]
                acc.leases -= 1
                freed.extend(self._free_retired_locked(acc))
        self._recycle(freed)

    def _free_retired_locked(self, acc: _Accelerator) -> list[_Block]:
        """Blocks no live view of ``acc`` can reach, already accounted as retired.

        An evicted accelerator keeps every block until its last lease ends, then gives
        all of them back; a resident one gives back each compaction-replaced block once
        no view older than its replacement remains.
        """
        if acc.evicted:
            if acc.leases:
                return []
            freed = [block for _gen, block in acc.retired] + acc.blocks
            acc.retired = []
            acc.blocks = []
            return freed
        oldest = min(acc.view_refs) if acc.view_refs else None
        freed = [block for gen, block in acc.retired if oldest is None or oldest >= gen]
        acc.retired = [
            (gen, block) for gen, block in acc.retired if oldest is not None and oldest < gen
        ]
        return freed

    # -- loading --------------------------------------------------------------------

    def _load(self, store: Path) -> _Accelerator:
        started = time.monotonic()
        with closing(_open_chunks_db_ro(chunks_db_path(store))) as conn:
            conn.execute("BEGIN")
            state = read_vector_feed_state(conn)
            if state is None:
                raise _Unavailable("feed-disabled")
            state.check()
            dim = declared_vector_dim(conn)
            if dim is None:
                raise IndexError("store has no vector table")
            count = state.vector_rows
            blocks = self._take_blocks(
                max(1, math.ceil(count / self.block_rows)), dim, "load", exclude=store
            )
            if blocks is None:
                raise _Unavailable("budget")
            check = _CHECK_ROWS * dim * 8
            if not self._reserve_scratch(check, exclude=store):
                self._give_back(blocks, "load")
                raise _Unavailable("budget")
            try:
                route = "supported"
                if _fast_qualified(conn):
                    try:
                        self._load_fast(conn, store, blocks, dim, count)
                        route = "fast"
                    except _FastLoadRejected:
                        self._count("loader.fast-rejected")
                        for block in blocks:
                            block.wipe()
                else:
                    self._count("loader.unqualified-version")
                if route == "supported":
                    self._load_supported(conn, store, blocks, dim, count)
                for block in blocks:
                    for start in range(0, block.used, _CHECK_ROWS):
                        rows = block.data[start : min(block.used, start + _CHECK_ROWS)]
                        if not np.isfinite(rows).all():
                            raise IndexError("store vectors contain non-finite values")
                        end = start + rows.shape[0]
                        block.wild[start:end] = _out_of_range(rows)
                        block.norm2[start:end] = np.einsum("ij,ij->i", rows, rows)
            except BaseException:
                self._give_back(blocks, "load")
                raise
            finally:
                self._release_scratch(check)
            acc = _Accelerator(
                store=store,
                epoch=state.epoch,
                dim=dim,
                base_seq=state.last_seq,
                route=route,
                load_seconds=time.monotonic() - started,
                blocks=blocks,
            )
            if acc.live_rows != count or len(acc.where) != count:
                self._give_back(blocks, "load")
                raise IndexError(
                    "vector cache load does not reconcile with the store's vector count"
                )
            return acc

    def _load_fast(
        self, conn: sqlite3.Connection, store: Path, blocks: list[_Block], dim: int, count: int
    ) -> None:
        width = dim * 4
        # The location map (one tuple per row) and one vector at a time, never a whole
        # sqlite-vec storage chunk.
        scratch = count * _ID_ROW_BYTES + width
        if not self._reserve_scratch(scratch, exclude=store):
            raise _Unavailable("budget")
        try:
            locations = conn.execute(_FAST_LOCATIONS_SQL).fetchall()
            if len(locations) != count or len({row[0] for row in locations}) != count:
                raise _FastLoadRejected()
            sizes = dict(conn.execute("SELECT chunk_id, size FROM chunks_vec_chunks").fetchall())
            block_index = 0
            position = 0
            while position < len(locations):
                vector_chunk = locations[position][1]
                end = position
                while end < len(locations) and locations[end][1] == vector_chunk:
                    end += 1
                size = sizes.get(vector_chunk)
                if not isinstance(size, int) or not isinstance(vector_chunk, int):
                    raise _FastLoadRejected()
                try:
                    blob = conn.blobopen(
                        "chunks_vec_vector_chunks00", "vectors", vector_chunk, readonly=True
                    )
                except sqlite3.Error as exc:
                    raise _FastLoadRejected() from exc
                with blob:
                    if len(blob) != size * width:
                        raise _FastLoadRejected()
                    for chunk_id, _chunk, offset in locations[position:end]:
                        if not isinstance(chunk_id, str) or not 0 <= offset < size:
                            raise _FastLoadRejected()
                        while blocks[block_index].used == blocks[block_index].capacity:
                            block_index += 1
                        blob.seek(offset * width)
                        vector = np.frombuffer(blob.read(width), dtype=np.float32)
                        blocks[block_index].append(chunk_id, vector, 0)
                position = end
            del locations
        finally:
            self._release_scratch(scratch)
        if count:
            step = max(1, count // _FAST_SAMPLES)
            for index in sorted({*range(0, count, step), count - 1}):
                block = blocks[index // self.block_rows]
                slot = index % self.block_rows
                chunk_id = block.ids[slot]
                via_sql = conn.execute(
                    "SELECT embedding FROM chunks_vec WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                if via_sql is None or bytes(via_sql[0]) != block.data[slot].tobytes():
                    raise _FastLoadRejected()

    def _load_supported(
        self, conn: sqlite3.Connection, store: Path, blocks: list[_Block], dim: int, count: int
    ) -> None:
        width = dim * 4
        if not self._reserve_scratch(width + _ID_ROW_BYTES, exclude=store):
            raise _Unavailable("budget")
        try:
            block_index = 0
            loaded = 0
            for chunk_id, payload in conn.execute("SELECT chunk_id, embedding FROM chunks_vec"):
                if len(payload) != width:
                    raise IndexError("store vector does not match the declared dimension")
                loaded += 1
                if loaded > count:
                    raise IndexError(
                        "vector cache load does not reconcile with the store's vector count"
                    )
                while blocks[block_index].used == blocks[block_index].capacity:
                    block_index += 1
                blocks[block_index].append(
                    str(chunk_id), np.frombuffer(payload, dtype=np.float32), 0
                )
            if loaded != count:
                raise IndexError(
                    "vector cache load does not reconcile with the store's vector count"
                )
        finally:
            self._release_scratch(width + _ID_ROW_BYTES)

    # -- refresh and compaction -----------------------------------------------------

    def _refresh(self, acc: _Accelerator, conn: sqlite3.Connection, state: VectorFeedState) -> None:
        """Apply the feed through the reading snapshot's position, then publish the new
        position and row counts together after they reconcile with the store's count."""
        seq = state.last_seq
        with acc.refresh_lock:
            if acc.evicted:
                raise _Unavailable("snapshot")
            if seq < acc.applied:
                return
            if seq == acc.applied:
                if acc.live_rows != state.vector_rows:
                    raise IndexError(
                        "vector cache does not reconcile with the store's vector count"
                    )
                return
            if acc.applied < state.floor_seq:
                acc.stale = "pruned"
                raise _Unavailable("snapshot")
            entries, inserts, first, last = conn.execute(
                f"SELECT count(*), "
                "coalesce(sum(op = 'insert' AND embedding IS NOT NULL), 0), min(seq), max(seq) "
                f"FROM {FEED_TABLE} WHERE seq > ? AND seq <= ?",
                (acc.applied, seq),
            ).fetchone()
            if entries != seq - acc.applied or first != acc.applied + 1 or last != seq:
                raise IndexError("vector feed has a gap between applied and snapshot positions")
            # Working memory: one streamed row and the bookkeeping for this interval.
            scratch = acc.dim * 4 + entries * _ID_ROW_BYTES
            if not self._reserve_scratch(scratch, exclude=acc.store):
                raise _Unavailable("budget")
            try:
                targets = self._room_for(acc, int(inserts))
                self._apply(acc, conn, state, targets)
            finally:
                self._release_scratch(scratch)
            self._compact(acc)

    def _room_for(self, acc: _Accelerator, inserts: int) -> list[tuple[_Block, int | None]]:
        """Slots for ``inserts`` new rows: free tail slots of every block, then fresh
        blocks; under memory pressure, slots of rows no lease can still see, reused in
        place only while this refresh is the accelerator's sole lease and no view exists.

        Each target is a block and either ``None`` (append) or a slot to overwrite.
        """
        targets: list[tuple[_Block, int | None]] = []
        free = 0
        for block in acc.blocks:
            room = block.capacity - block.used
            targets.extend((block, None) for _ in range(min(room, inserts - free)))
            free += room
            if free >= inserts:
                return targets
        short = inserts - len(targets)
        needed = math.ceil(short / self.block_rows)
        fresh = self._take_blocks(needed, acc.dim, "live", exclude=acc.store)
        if fresh is not None:
            with self._lock:
                if acc.evicted:
                    moved = len(fresh) * self._block_bytes(acc.dim)
                    self._bytes["live"] -= moved
                    self._bytes["retired"] += moved
                acc.blocks.extend(fresh)
            for block in fresh:
                targets.extend((block, None) for _ in range(min(block.capacity, short)))
                short -= block.capacity
            return targets
        with self._lock:
            sole = acc.leases == 1 and not acc.view_refs and not acc.retired
        if sole:
            reusable = [
                (block, int(slot))
                for block in acc.blocks
                for slot in np.flatnonzero(block.dead[: block.used] != _LIVE)
            ]
            if len(reusable) >= short:
                self._count("reclaimed_slots")
                for block, slot in reusable[:short]:
                    chunk_id = block.ids[slot]
                    assert chunk_id is not None
                    entries = acc.where[chunk_id]
                    entries.remove((block, slot))
                    if not entries:
                        del acc.where[chunk_id]
                    block.dead_rows -= 1
                    targets.append((block, slot))
                return targets
        raise _Unavailable("budget")

    def _apply(
        self,
        acc: _Accelerator,
        conn: sqlite3.Connection,
        state: VectorFeedState,
        targets: list[tuple[_Block, int | None]],
    ) -> None:
        seq = state.last_seq
        width = acc.dim * 4
        cleared: dict[str, int] = {}
        # Reclaimed slots held dead rows; the count drops when the new state publishes.
        reclaimed = sum(1 for _block, slot in targets if slot is not None)
        live, dead = acc.live_rows, acc.dead_rows - reclaimed
        exact_from = acc.exact_from
        next_target = 0
        cursor = conn.execute(
            f"SELECT seq, op, chunk_id, embedding FROM {FEED_TABLE} "
            "WHERE seq > ? AND seq <= ? ORDER BY seq",
            (acc.applied, seq),
        )
        for position, op, chunk_id, payload in cursor:
            if op == "insert":
                if payload is None:
                    cleared[chunk_id] = position
                    continue
                if acc.live_slot(chunk_id) is not None:
                    raise IndexError("vector feed inserts a chunk that is already live")
                if len(payload) != width:
                    raise IndexError("vector feed payload does not match the vector dimension")
                vector = np.frombuffer(payload, dtype=np.float32)
                if not np.isfinite(vector).all():
                    raise IndexError("vector feed payload contains non-finite values")
                block, slot = targets[next_target]
                next_target += 1
                if slot is None:
                    slot = block.append(chunk_id, vector, position)
                else:
                    block.put(slot, chunk_id, vector, position)
                acc.where.setdefault(chunk_id, []).append((block, slot))
                live += 1
            elif op == "delete":
                if chunk_id in cleared:
                    # The insert's payload was cleared by this delete's transaction, so
                    # no snapshot from the insert up to this delete is representable.
                    del cleared[chunk_id]
                    exact_from = max(exact_from, position)
                    continue
                located = acc.live_slot(chunk_id)
                if located is None:
                    raise IndexError("vector feed deletes a chunk that is not live")
                block, slot = located
                block.dead[slot] = position
                block.dead_rows += 1
                live -= 1
                dead += 1
            else:
                raise IndexError("vector feed holds an unknown operation")
        if cleared:
            raise IndexError("vector feed holds a cleared insert without its delete")
        if live != state.vector_rows:
            raise IndexError("vector cache does not reconcile with the store's vector count")
        with self._lock:
            acc.exact_from = exact_from
            acc.live_rows = live
            acc.dead_rows = dead
            acc.applied = seq

    def _compact(self, acc: _Accelerator) -> None:
        """Rewrite at most one block without the rows no live lease can still see.

        Runs under the refresh lock. Skipped while an earlier replacement is still held
        by an older view, so at most one replaced block per store is outstanding, and its
        survivors are copied in bounded slices under a scratch reservation.
        """
        with self._lock:
            if acc.evicted or acc.retired:
                return
            horizon = min(acc.horizons) if acc.horizons else acc.applied
        candidates = [
            block
            for block in acc.blocks
            if block.dead_rows >= VECTOR_CACHE_COMPACT_DEAD_FRACTION * block.capacity
        ]
        if not candidates:
            return
        old = max(candidates, key=lambda block: block.dead_rows)
        keep = np.flatnonzero(old.dead[: old.used] > horizon)
        dropped = old.used - keep.size
        if dropped == 0:
            return
        scratch = _COPY_ROWS * (acc.dim * 4 + 8) + old.used * 8
        if not self._reserve_scratch(scratch, exclude=acc.store):
            return
        try:
            replacement: _Block | None = None
            if keep.size:
                taken = self._take_blocks(1, acc.dim, "live", exclude=acc.store)
                if taken is None:
                    return
                replacement = taken[0]
                for start in range(0, keep.size, _COPY_ROWS):
                    part = keep[start : start + _COPY_ROWS]
                    end = start + part.size
                    replacement.data[start:end] = old.data[part]
                    replacement.norm2[start:end] = old.norm2[part]
                    replacement.born[start:end] = old.born[part]
                    replacement.dead[start:end] = old.dead[part]
                    replacement.wild[start:end] = old.wild[part]
                replacement.ids[: keep.size] = [old.ids[int(slot)] for slot in keep]
                replacement.used = int(keep.size)
                replacement.dead_rows = int(
                    np.count_nonzero(replacement.dead[: keep.size] != _LIVE)
                )
        finally:
            self._release_scratch(scratch)
        moved = {int(slot): new for new, slot in enumerate(keep)}
        for slot in range(old.used):
            chunk_id = old.ids[slot]
            assert chunk_id is not None
            entries = acc.where[chunk_id]
            index = entries.index((old, slot))
            if slot in moved:
                assert replacement is not None
                entries[index] = (replacement, moved[slot])
            else:
                entries.pop(index)
                if not entries:
                    del acc.where[chunk_id]
        freed: list[_Block] = []
        size = self._block_bytes(acc.dim)
        with self._lock:
            position = next(i for i, block in enumerate(acc.blocks) if block is old)
            if replacement is None:
                acc.blocks.pop(position)
            else:
                acc.blocks[position] = replacement
            acc.dead_rows -= dropped
            acc.view_gen += 1
            acc.retired.append((acc.view_gen, old))
            if not acc.evicted:
                self._bytes["live"] -= size
                self._bytes["retired"] += size
            elif replacement is not None:
                # Evicted while copying: the replacement belongs to the retired set too.
                self._bytes["live"] -= size
                self._bytes["retired"] += size
            self._counters["compactions"] += 1
            freed = self._free_retired_locked(acc)
        self._recycle(freed)


def _fast_qualified(conn: sqlite3.Connection) -> bool:
    version = conn.execute("SELECT vec_version()").fetchone()[0]
    if version != QUALIFIED_SQLITE_VEC_VERSION:
        return False
    try:
        info = dict(conn.execute("SELECT key, value FROM chunks_vec_info").fetchall())
    except sqlite3.Error:
        return False
    return info.get("CREATE_VERSION") == QUALIFIED_SQLITE_VEC_VERSION
