"""Bounded document admission, fair embedding turns and atomic publication."""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from palace.index._errors import IndexError
from palace.index.config import (
    REMOTE_EMBED_MAX_BATCH,
    REMOTE_EMBED_MAX_TOKEN_BUDGET,
    EmbeddingIdentity,
)
from palace.index.core import FilePlan, commit_plan, delete_path, plan_one
from palace.index.egress import EmbeddingUsage, current_usage, egress_context, usage_scope
from palace.index.embedder import Embedder, request_units
from palace.index.pipeline import classify_kind
from palace.index.staging import Prepared, ReceiptVectors, SourceBinding, Staging, digest
from palace.reindex._errors import ReindexError
from palace.reindex.bootstrap import enumerate_root
from palace.watch.ignore import IgnoreEngine


@dataclass(frozen=True)
class BuildLimits:
    """Supported-job ceilings, independently enforced before inference."""

    source_bytes: int = 8 * 1024 * 1024
    chunks: int = 16384
    plan_bytes: int = 64 * 1024 * 1024
    max_files: int = 100000
    staging_bytes: int = 1024 * 1024 * 1024
    turn_inputs: int = 32
    turn_bytes: int = 128 * 1024
    remote_turn_inputs: int = REMOTE_EMBED_MAX_BATCH

    def __post_init__(self) -> None:
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in asdict(self).values()
        ):
            raise IndexError("build limits must be positive integers")


@dataclass(frozen=True)
class BuildProgress:
    event: str
    watch_root: str | None = None
    path: str | None = None
    documents: int | None = None
    committed: int = 0
    chunks: int = 0
    reused: int = 0


@dataclass
class RootStats:
    root: Path
    usage: EmbeddingUsage
    files: int = 0
    embedded: int = 0
    unchanged: int = 0
    deleted: int = 0
    started: float | None = None
    finished: float | None = None

    def begin(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished if self.finished is not None else time.monotonic()) - self.started


@dataclass
class _Document:
    ref: Prepared
    root: RootStats
    turns: list[tuple[int, int]]
    next_turn: int = 0
    active: bool = False
    done: bool = False


@dataclass
class ScheduledBuild:
    roots: list[RootStats]
    staging: Staging
    refs: list[Prepared] = field(default_factory=list)

    def clear_completed(self) -> None:
        self.staging.clear_completed(self.refs)


def _ranges(inputs: Sequence[str], limits: BuildLimits, remote: bool) -> list[tuple[int, int]]:
    result = []
    start = 0
    maximum = (
        min(limits.remote_turn_inputs, REMOTE_EMBED_MAX_BATCH) if remote else limits.turn_inputs
    )
    while start < len(inputs):
        end, size = start, 0
        while end < len(inputs) and end - start < maximum:
            text = inputs[end]
            raw = len(text.encode("utf-8"))
            units = request_units(text) if remote else raw
            cap = (
                min(limits.turn_bytes, REMOTE_EMBED_MAX_TOKEN_BUDGET)
                if remote
                else limits.turn_bytes
            )
            if raw > limits.turn_bytes or units > cap:
                raise IndexError(
                    "single embedding input exceeds build turn limit; "
                    "ignore the source or raise the supported limit"
                )
            if size + units > cap:
                break
            size += units
            end += 1
        result.append((start, end))
        start = end
    return result


def _source(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    limits: BuildLimits,
    full: bool,
    binding: SourceBinding | None = None,
) -> tuple[FilePlan, SourceBinding]:
    kind = classify_kind(path)
    stored = path.relative_to(root).as_posix()
    metadata_size = conn.execute(
        "SELECT length(CAST(sections_json AS BLOB)) FROM files WHERE watch_root=? AND path=?",
        (str(root), stored),
    ).fetchone()
    if metadata_size is not None and metadata_size[0] > limits.plan_bytes:
        raise IndexError(f"prior document metadata exceeds plan_bytes: {stored}")
    offset = 0
    if kind == "jsonl" and not full:
        row = conn.execute(
            "SELECT last_byte_offset FROM jsonl_cursors WHERE watch_root=? AND path=?",
            (str(root), stored),
        ).fetchone()
        offset = int(row[0]) if row else 0
    data: bytes | None = None
    if kind in {"markdown", "code", "text", "jsonl"}:
        if binding is None:
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read(limits.source_bytes + 1)
            except FileNotFoundError as exc:
                raise IndexError(
                    f"source vanished before admission: {stored}; rerun the build"
                ) from exc
            if len(data) > limits.source_bytes:
                raise IndexError(
                    f"source admission limit exceeded: {stored} ({limits.source_bytes} bytes); "
                    "old rows remain visible; ignore the source or adjust source_bytes "
                    "through Python BuildLimits"
                )
        else:
            if offset != binding.offset:
                raise IndexError(f"JSONL cursor changed since admission: {stored}")
            data = binding.read(limits.source_bytes)
    plan = plan_one(
        conn=conn,
        file_kind=kind,
        change_kind="created",
        path=path,
        watch_root=root,
        full=full,
        log=lambda _: None,
        source_bytes=data,
    )
    if len(plan.rows) > limits.chunks:
        raise IndexError(
            f"document chunk admission limit exceeded: {stored}; "
            "ignore the source or adjust chunks through Python BuildLimits"
        )
    if data is None:
        source = SourceBinding(str(root), stored, 0, 0, digest(b""), False)
    else:
        length = (plan.jsonl_offset - offset) if plan.jsonl_offset is not None else len(data)
        source = SourceBinding(
            str(root), stored, offset, length, digest(data[:length]), kind == "jsonl"
        )
    if binding is not None and source != binding:
        raise IndexError(f"source snapshot changed: {stored}")
    return plan, source


def schedule(
    *,
    conn: sqlite3.Connection,
    store: Path,
    roots: Sequence[tuple[Path, IgnoreEngine]],
    embedder: Embedder,
    identity: EmbeddingIdentity,
    full: bool,
    concurrency: int,
    limits: BuildLimits,
    log: Callable[[str], None],
    progress: Callable[[BuildProgress], None] | None,
    remote: bool,
    verbose: bool = False,
) -> ScheduledBuild:
    """The caller holds the whole-build lock; no SQLite connection crosses threads."""
    maximum = (
        min(limits.remote_turn_inputs, REMOTE_EMBED_MAX_BATCH) if remote else limits.turn_inputs
    )
    small_reserve = 2 * limits.plan_bytes + maximum * (identity.dim * 4 + 16384) + 16384
    if small_reserve >= limits.staging_bytes:
        raise IndexError(
            "staging_bytes must exceed the maximum one-turn reservation; "
            "increase it or lower plan/turn limits"
        )
    staging = Staging(
        store=store,
        budget=limits.staging_bytes,
        plan_cap=limits.plan_bytes,
        dim=identity.dim,
        identity=asdict(identity),
        partition=(maximum, limits.turn_bytes),
        log=log,
    )
    stats = [RootStats(root.resolve(), EmbeddingUsage(parent=current_usage())) for root, _ in roots]
    output = ScheduledBuild(stats, staging)
    documents: list[_Document] = []
    all_paths: dict[str, set[str]] = {str(s.root): set() for s in stats}
    count: int | None = None
    committed = reused = completed_chunks = 0
    last_log = -math.inf
    commit_log = log if verbose else (lambda _: None)

    def emit(event: str, document: _Document | None = None) -> None:
        nonlocal last_log
        item = BuildProgress(
            event,
            str(document.root.root) if document else None,
            document.ref.source.path if document else None,
            count,
            committed,
            completed_chunks,
            reused,
        )
        if progress is not None:
            progress(item)
        now = time.monotonic()
        if event in {"refused", "complete"} or now - last_log >= 1.0:
            discovered = count if count is not None else "unknown"
            log(
                f"palace index progress: event={event} documents={discovered} "
                f"committed={committed} "
                f"chunks={completed_chunks} reused={reused}"
            )
            last_log = now

    try:
        # Discovery is bounded before the existing globally sorted collector appends.
        pairs: list[tuple[RootStats, Path]] = []
        for state, (_, engine) in zip(stats, roots, strict=True):
            try:
                for relative, absolute in enumerate_root(
                    root=state.root,
                    engine=engine,
                    resume_from=None,
                    max_files=limits.max_files - len(pairs),
                ):
                    pairs.append((state, absolute))
                    all_paths[str(state.root)].add(relative.as_posix())
            except ReindexError as exc:
                raise IndexError(str(exc)) from exc
        count = len(pairs)
        emit("discovered")
        for state, path in pairs:
            state.files += 1
            plan, source = _source(conn, state.root, path, limits, full)
            if not plan.rows:
                state.begin()
                outcome = commit_plan(
                    conn=conn,
                    plan=plan,
                    embeddings=[],
                    identity=None if full else identity,
                    log=commit_log,
                )
                if outcome.noop and not plan.full_delete:
                    state.unchanged += 1
                state.finished = time.monotonic()
                continue
            turns = _ranges(plan.embed_inputs, limits, remote)
            ref, _ = staging.prepare(plan, source, small=len(turns) == 1)
            documents.append(_Document(ref, state, turns))
            output.refs.append(ref)
        staging.gc({d.ref.key for d in documents}, set(all_paths))
        # Refuse a known unsupported document before spending on any other one.
        for document in documents:
            staging.reservation_ceiling(document.ref, small_reserve)
        emit("admitted-manifest")

        def embed(texts: list[str], ids: list[str], document: _Document) -> list[list[float]]:
            with (
                usage_scope(document.root.usage),
                egress_context(watch_root=str(document.root.root), chunk_ids=ids),
            ):
                return embedder.embed(texts)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            queued: list[tuple[_Document, int, int, Future[list[list[float]]] | None]] = []

            def fold() -> None:
                nonlocal committed, completed_chunks, reused
                # Retain successful responses from this bounded group before surfacing
                # its first failure; no document publishes from a failed group.
                failure: Exception | None = None
                for document, start, end, future in queued:
                    if future is not None:
                        try:
                            vectors = future.result()
                        except Exception as exc:
                            error = IndexError(
                                f"embedding failed for {document.ref.source.path}: {exc}"
                            )
                            error.__cause__ = exc
                            failure = failure or error
                            continue
                        try:
                            staging.save(document.ref, start, end, vectors)
                        except Exception as exc:
                            failure = failure or exc
                            continue
                        completed_chunks += end - start
                        emit("receipt", document)
                    else:
                        reused += end - start
                        emit("reused", document)
                if failure is not None:
                    raise failure
                for document, _start, _end, _future in queued:
                    document.next_turn += 1
                    if document.next_turn == len(document.turns):
                        document.ref.source.read(limits.source_bytes)
                        plan = staging.plan(document.ref)
                        receipts = ReceiptVectors(staging, document.ref, document.turns)
                        outcome = commit_plan(
                            conn=conn,
                            plan=plan,
                            embeddings=receipts,
                            identity=None if full else identity,
                            log=commit_log,
                        )
                        staging.publish(document.ref, plan, conn)
                        document.root.embedded += int(outcome.embedded > 0)
                        document.root.finished = time.monotonic()
                        document.done = True
                        committed += 1
                        emit("committed", document)
                queued.clear()

            while any(not d.done for d in documents):
                serviced = False
                for document in documents:
                    if document.done:
                        continue
                    if not document.active:
                        if not staging.can_admit(document.ref, small_reserve):
                            continue
                        plan, source = _source(
                            conn,
                            document.root.root,
                            document.root.root / document.ref.source.path,
                            limits,
                            full,
                            document.ref.source,
                        )
                        ref, body = staging.prepare(plan, source, small=len(document.turns) == 1)
                        if ref != document.ref:
                            raise IndexError("document plan changed after admission")
                        document.root.begin()
                        if staging.published(ref, plan, conn):
                            document.done = True
                            document.root.unchanged += 1
                            document.root.finished = time.monotonic()
                            reused += ref.count
                            emit("reused-publication", document)
                            serviced = True
                            continue
                        staging.admit(ref, body, plan)
                        document.active = True
                        emit("admitted", document)
                    start, end = document.turns[document.next_turn]
                    receipt = staging.receipt(document.ref, start, end)
                    future = None
                    if receipt is None:
                        texts, ids = staging.inputs(document.ref, start, end)
                        future = pool.submit(embed, texts, ids, document)
                        emit("dispatched", document)
                    queued.append((document, start, end, future))
                    serviced = True
                    if len(queued) == concurrency:
                        fold()
                fold()
                if not serviced:
                    raise IndexError(
                        "staging budget cannot admit pending work; "
                        "restore previous limits or remove derived build staging"
                    )
        # Only a fully successful manifest reaches deletion reconciliation.
        for state in stats:
            missing = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT path FROM files WHERE watch_root=?", (str(state.root),)
                )
                if row[0] not in all_paths[str(state.root)]
            )
            for stored in missing:
                state.begin()
                delete_path(conn=conn, watch_root=str(state.root), path_str=stored)
                state.deleted += 1
            if missing:
                state.finished = time.monotonic()
        emit("complete")
        return output
    except BaseException:
        emit("refused")
        raise
