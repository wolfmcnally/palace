"""Shared planning and single-writer commit core for palace's index.

The daemon and the per-file update use :func:`index_one`, which plans and
embeds outside the store's writer lock and holds the lock only to commit. The
synchronous builder uses the same :func:`plan_one` and :func:`commit_plan`
halves under a lock it holds for its whole reconcile, so only remote embedding
may run concurrently; every SQLite read and write remains on the calling
writer thread.

Every plan records the prior file state it was computed against (read from
the store before the source bytes, so a competing commit between the two reads
is visible). :func:`commit_plan` re-reads that state inside its transaction and
raises :class:`StalePlanError` when it moved; :func:`index_one` then plans
again against disk and the store as they are now. A delete plan is stale when
the file exists again at commit time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import sqlite_vec

from palace.daemons.capture.config import BOISE_TZ
from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError, StalePlanError
from palace.index.code import CodeChunk, parse_code
from palace.index.config import MAX_STALE_REPLANS, EmbeddingIdentity
from palace.index.egress import EgressContext, egress_context
from palace.index.embedder import Embedder
from palace.index.enrich import scored_text
from palace.index.jsonl import JsonlChunk, parse_jsonl_tail
from palace.index.markdown import Section, parse_markdown
from palace.index.schema import assert_identity, compute_chunk_id, ensure_fts_rowid_map
from palace.index.text import looks_binary, parse_text
from palace.reindex.config import ChangeKind
from palace.writer_lock import WRITER_LOCK_TIMEOUT_SECONDS, writer_lock

__all__ = [
    "FilePlan",
    "IndexOutcome",
    "commit_plan",
    "delete_path",
    "index_one",
    "plan_one",
]


@dataclass(frozen=True)
class IndexOutcome:
    """Counters returned after indexing or deleting one path."""

    kind: str
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    embedded: int = 0
    noop: bool = False
    noop_reason: str | None = None


@dataclass(frozen=True)
class PendingRow:
    """One chunk row prepared before its vector exists."""

    chunk_id: str
    section_index: int
    window_index: int
    heading: str | None
    heading_depth: int
    body: str
    body_hash: str
    frontmatter_json: str | None


@dataclass(frozen=True)
class PendingFileRow:
    """One ``files`` row and the per-file metadata copied to chunks."""

    path: str
    watch_root: str
    kind: str
    file_hash: str
    sections_json: str
    wikilinks_json: str = "[]"
    tags_json: str = "[]"
    dataview_fields_json: str = "[]"
    embeds_json: str = "[]"


@dataclass(frozen=True)
class FilePlan:
    """Immutable value crossing the producer → embed → writer seam."""

    kind: str
    stored: str
    rel: str
    watch_root: str
    noop: IndexOutcome | None
    embed_inputs: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    rows: tuple[PendingRow, ...]
    stale_chunk_ids: tuple[str, ...]
    file_row: PendingFileRow | None
    jsonl_offset: int | None
    full_delete: bool
    counts: IndexOutcome
    # The store state this plan was computed against, or ``None`` when there
    # is nothing to verify: full plans replace unconditionally, delete plans
    # verify the file's absence instead, and noop plans never commit.
    prior_signature: str | None

    def __post_init__(self) -> None:
        if not (len(self.embed_inputs) == len(self.chunk_ids) == len(self.rows)):
            raise ValueError("file plan embed inputs, chunk ids, and rows must have equal lengths")


def _now_iso() -> str:
    return datetime.now(BOISE_TZ).isoformat(timespec="seconds")


def _stored_path(path: Path, watch_root: Path) -> str:
    try:
        return path.relative_to(watch_root).as_posix()
    except ValueError as exc:
        # Never fall back to an absolute path: that would make the shipped
        # chunks database depend on its build machine and break relocation.
        raise IndexError(f"path {path} is not under watch_root {watch_root}") from exc


def _log_relative_path(path: Path, watch_root: Path) -> str:
    try:
        return path.relative_to(watch_root).as_posix()
    except ValueError:
        return path.as_posix()


class _DiffableChunk(Protocol):
    @property
    def section_index(self) -> int: ...

    @property
    def window_index(self) -> int: ...

    @property
    def body_hash(self) -> str: ...

    @property
    def body(self) -> str: ...


@dataclass(frozen=True)
class _SectionDiff:
    unchanged: tuple[_DiffableChunk, ...]
    changed: tuple[_DiffableChunk, ...]
    new: tuple[_DiffableChunk, ...]
    removed: tuple[tuple[int, int, str], ...]
    prior_chunk_ids_by_position: dict[tuple[int, int], str]


def _noop_plan(
    *,
    kind: str,
    stored: str,
    rel: str,
    watch_root: Path,
    reason: str,
    full_delete: bool,
) -> FilePlan:
    outcome = IndexOutcome(kind=kind, noop=True, noop_reason=reason)
    return FilePlan(
        kind=kind,
        stored=stored,
        rel=rel,
        watch_root=str(watch_root),
        noop=outcome,
        embed_inputs=(),
        chunk_ids=(),
        rows=(),
        stale_chunk_ids=(),
        file_row=None,
        jsonl_offset=None,
        full_delete=full_delete,
        counts=outcome,
        prior_signature=None,
    )


def _prior_file_row(
    conn: sqlite3.Connection, *, watch_root: Path, stored: str
) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT file_hash, sections_json FROM files WHERE watch_root = ? AND path = ?",
        (str(watch_root), stored),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _file_signature(prior: tuple[str, str] | None) -> str:
    return "absent" if prior is None else prior[0] + "\n" + prior[1]


def _jsonl_signature(prior_sections: str | None, resume_offset: int) -> str:
    return (prior_sections if prior_sections is not None else "absent") + "\n" + str(resume_offset)


def _current_signature(conn: sqlite3.Connection, plan: FilePlan) -> str:
    """Re-read the state :func:`plan_one` recorded, inside the commit transaction."""
    watch_root = Path(plan.watch_root)
    if plan.kind != "jsonl":
        return _file_signature(_prior_file_row(conn, watch_root=watch_root, stored=plan.stored))
    cursor_row = conn.execute(
        "SELECT last_byte_offset FROM jsonl_cursors WHERE watch_root = ? AND path = ?",
        (plan.watch_root, plan.stored),
    ).fetchone()
    prior_row = conn.execute(
        "SELECT sections_json FROM files WHERE watch_root = ? AND path = ?",
        (plan.watch_root, plan.stored),
    ).fetchone()
    return _jsonl_signature(
        None if prior_row is None else str(prior_row[0]),
        int(cursor_row[0]) if cursor_row is not None else 0,
    )


def plan_one(
    *,
    conn: sqlite3.Connection,
    file_kind: str,
    change_kind: ChangeKind,
    path: Path,
    watch_root: Path,
    full: bool = False,
    verbose: bool = False,
    log: Callable[[str], None],
) -> FilePlan:
    """Read and diff one file without embedding or mutating SQLite."""
    stored = _stored_path(path, watch_root)
    rel = _log_relative_path(path, watch_root)
    if change_kind == "deleted":
        if path.exists():
            log(f"palace index: skip-delete path={rel} reason=path-still-exists")
            return _noop_plan(
                kind="deleted",
                stored=stored,
                rel=rel,
                watch_root=watch_root,
                reason="path-still-exists",
                full_delete=False,
            )
        outcome = IndexOutcome(kind="deleted")
        return FilePlan(
            kind="deleted",
            stored=stored,
            rel=rel,
            watch_root=str(watch_root),
            noop=None,
            embed_inputs=(),
            chunk_ids=(),
            rows=(),
            stale_chunk_ids=(),
            file_row=None,
            jsonl_offset=None,
            full_delete=True,
            counts=outcome,
            prior_signature=None,
        )
    if file_kind == "markdown":
        return _plan_markdown(
            conn=conn, path=path, watch_root=watch_root, full=full, verbose=verbose, log=log
        )
    if file_kind == "jsonl":
        return _plan_jsonl(conn=conn, path=path, watch_root=watch_root, full=full, log=log)
    if file_kind == "code":
        return _plan_code(
            conn=conn, path=path, watch_root=watch_root, full=full, verbose=verbose, log=log
        )
    if file_kind == "text":
        return _plan_text(
            conn=conn, path=path, watch_root=watch_root, full=full, verbose=verbose, log=log
        )
    log(f"palace index: skip kind={file_kind} path={rel}")
    return _noop_plan(
        kind=file_kind,
        stored=stored,
        rel=rel,
        watch_root=watch_root,
        reason="skip-kind",
        full_delete=full,
    )


def _plan_markdown(
    *,
    conn: sqlite3.Connection,
    path: Path,
    watch_root: Path,
    full: bool,
    verbose: bool,
    log: Callable[[str], None],
) -> FilePlan:
    stored = _stored_path(path, watch_root)
    rel = _log_relative_path(path, watch_root)
    if not path.is_file():
        log(f"palace index: missing path={rel} reason=file-vanished")
        return _noop_plan(
            kind="markdown",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-vanished",
            full_delete=full,
        )
    # Store state first, source bytes second: a commit that lands between
    # the two reads then shows up as a signature mismatch at commit time.
    prior = _prior_file_row(conn, watch_root=watch_root, stored=stored)
    prior_signature = None if full else _file_signature(prior)
    file_bytes = path.read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    if not full and prior is not None and prior[0] == file_hash:
        if verbose:
            log(f"palace index: noop path={rel} reason=file-hash-unchanged")
        return _noop_plan(
            kind="markdown",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-hash-unchanged",
            full_delete=False,
        )
    parsed = parse_markdown(absolute_path=path, file_bytes=file_bytes)
    prior_sections = None if full or prior is None else prior[1]
    diff = _diff_sections(new_sections=parsed.sections, prior_sections_json=prior_sections)
    to_embed = (*diff.new, *diff.changed)
    chunk_ids = tuple(
        compute_chunk_id(
            watch_root=str(watch_root),
            path=stored,
            section_index=section.section_index,
            window_index=section.window_index,
            body_hash=section.body_hash,
        )
        for section in to_embed
    )
    frontmatter_json = (
        json.dumps(parsed.frontmatter, sort_keys=True, separators=(",", ":"))
        if parsed.frontmatter
        else None
    )
    rows = tuple(
        PendingRow(
            chunk_id=chunk_id,
            section_index=section.section_index,
            window_index=section.window_index,
            heading=section.heading if isinstance(section, Section) else None,
            heading_depth=section.heading_depth if isinstance(section, Section) else 0,
            body=section.body,
            body_hash=section.body_hash,
            frontmatter_json=frontmatter_json,
        )
        for section, chunk_id in zip(to_embed, chunk_ids, strict=True)
    )
    counts = _counts("markdown", diff)
    return FilePlan(
        kind="markdown",
        stored=stored,
        rel=rel,
        watch_root=str(watch_root),
        noop=None,
        embed_inputs=tuple(
            scored_text(
                path=stored,
                heading=section.heading if isinstance(section, Section) else None,
                body=section.body,
            )
            for section in to_embed
        ),
        chunk_ids=chunk_ids,
        rows=rows,
        stale_chunk_ids=_stale_chunk_ids(diff),
        file_row=PendingFileRow(
            path=stored,
            watch_root=str(watch_root),
            kind="markdown",
            file_hash=file_hash,
            sections_json=_build_sections_json(
                unchanged=diff.unchanged,
                inserted=tuple(zip(to_embed, chunk_ids, strict=True)),
                prior_chunk_ids_by_position=diff.prior_chunk_ids_by_position,
            ),
            wikilinks_json=json.dumps(list(parsed.wikilinks), separators=(",", ":")),
            tags_json=json.dumps(list(parsed.tags), separators=(",", ":")),
            dataview_fields_json=json.dumps(
                [list(pair) for pair in parsed.dataview_fields], separators=(",", ":")
            ),
            embeds_json=json.dumps(list(parsed.embeds), separators=(",", ":")),
        ),
        jsonl_offset=None,
        full_delete=full,
        counts=counts,
        prior_signature=prior_signature,
    )


def _plan_jsonl(
    *,
    conn: sqlite3.Connection,
    path: Path,
    watch_root: Path,
    full: bool,
    log: Callable[[str], None],
) -> FilePlan:
    stored = _stored_path(path, watch_root)
    rel = _log_relative_path(path, watch_root)
    if not path.is_file():
        log(f"palace index: missing path={rel} reason=file-vanished")
        return _noop_plan(
            kind="jsonl",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-vanished",
            full_delete=full,
        )
    cursor_row = (
        None
        if full
        else conn.execute(
            "SELECT last_byte_offset FROM jsonl_cursors WHERE watch_root = ? AND path = ?",
            (str(watch_root), stored),
        ).fetchone()
    )
    resume_offset = int(cursor_row[0]) if cursor_row is not None else 0
    prior_row = (
        None
        if full
        else conn.execute(
            "SELECT sections_json FROM files WHERE watch_root = ? AND path = ?",
            (str(watch_root), stored),
        ).fetchone()
    )
    prior_sections = prior_row[0] if prior_row is not None else None
    prior_signature = None if full else _jsonl_signature(prior_sections, resume_offset)
    with path.open("rb") as handle:
        handle.seek(resume_offset)
        tail_bytes = handle.read()
    new_chunks, advance = parse_jsonl_tail(file_bytes=tail_bytes, resume_offset=0, log=log)
    prior_count = _count_prior_jsonl_records(prior_sections)
    offset_chunks = tuple(
        JsonlChunk(
            record_index=chunk.record_index + prior_count,
            window_index=chunk.window_index,
            body=chunk.body,
            body_hash=chunk.body_hash,
            record_id=chunk.record_id,
            section_index=chunk.section_index + prior_count,
        )
        for chunk in new_chunks
    )
    diff = _diff_sections(
        new_sections=_synthesize_prior_chunks(prior_sections) + offset_chunks,
        prior_sections_json=prior_sections,
    )
    to_embed = (*diff.new, *diff.changed)
    chunk_ids = tuple(
        _compute_jsonl_chunk_id(watch_root=str(watch_root), path=stored, chunk=section)
        for section in to_embed
        if isinstance(section, JsonlChunk)
    )
    rows = tuple(
        PendingRow(
            chunk_id=chunk_id,
            section_index=section.section_index,
            window_index=section.window_index,
            heading=None,
            heading_depth=0,
            body=section.body,
            body_hash=section.body_hash,
            frontmatter_json=(
                json.dumps(
                    {"jsonl_record_id": section.record_id},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if isinstance(section, JsonlChunk) and section.record_id is not None
                else None
            ),
        )
        for section, chunk_id in zip(to_embed, chunk_ids, strict=True)
    )
    return FilePlan(
        kind="jsonl",
        stored=stored,
        rel=rel,
        watch_root=str(watch_root),
        noop=None,
        embed_inputs=tuple(scored_text(path=stored, heading=None, body=row.body) for row in rows),
        chunk_ids=chunk_ids,
        rows=rows,
        stale_chunk_ids=_stale_chunk_ids(diff),
        file_row=PendingFileRow(
            path=stored,
            watch_root=str(watch_root),
            kind="jsonl",
            file_hash="-",
            sections_json=_build_sections_json(
                unchanged=diff.unchanged,
                inserted=tuple(zip(to_embed, chunk_ids, strict=True)),
                prior_chunk_ids_by_position=diff.prior_chunk_ids_by_position,
            ),
        ),
        jsonl_offset=resume_offset + advance,
        full_delete=full,
        counts=_counts("jsonl", diff),
        prior_signature=prior_signature,
    )


def _plan_code(
    *,
    conn: sqlite3.Connection,
    path: Path,
    watch_root: Path,
    full: bool,
    verbose: bool,
    log: Callable[[str], None],
) -> FilePlan:
    stored = _stored_path(path, watch_root)
    rel = _log_relative_path(path, watch_root)
    if not path.is_file():
        log(f"palace index: missing path={rel} reason=file-vanished")
        return _noop_plan(
            kind="code",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-vanished",
            full_delete=full,
        )
    prior = _prior_file_row(conn, watch_root=watch_root, stored=stored)
    prior_signature = None if full else _file_signature(prior)
    file_bytes = path.read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    if not full and prior is not None and prior[0] == file_hash:
        if verbose:
            log(f"palace index: noop path={rel} reason=file-hash-unchanged")
        return _noop_plan(
            kind="code",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-hash-unchanged",
            full_delete=False,
        )
    chunks = tuple(parse_code(absolute_path=path, file_bytes=file_bytes))
    diff = _diff_sections(
        new_sections=chunks,
        prior_sections_json=None if full or prior is None else prior[1],
    )
    to_embed = (*diff.new, *diff.changed)
    chunk_ids = tuple(_standard_chunk_id(watch_root, stored, section) for section in to_embed)
    rows = tuple(
        PendingRow(
            chunk_id=chunk_id,
            section_index=section.section_index,
            window_index=section.window_index,
            heading=section.symbol if isinstance(section, CodeChunk) else None,
            heading_depth=0,
            body=section.body,
            body_hash=section.body_hash,
            frontmatter_json=(
                json.dumps(
                    {
                        "symbol": section.symbol,
                        "kind": section.kind,
                        "language": section.language,
                        "start_line": section.start_line,
                        "end_line": section.end_line,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if isinstance(section, CodeChunk) and section.symbol is not None
                else None
            ),
        )
        for section, chunk_id in zip(to_embed, chunk_ids, strict=True)
    )
    return _ordinary_plan(
        kind="code",
        stored=stored,
        rel=rel,
        watch_root=watch_root,
        full=full,
        file_hash=file_hash,
        diff=diff,
        to_embed=to_embed,
        chunk_ids=chunk_ids,
        rows=rows,
        headings=tuple(row.heading for row in rows),
        prior_signature=prior_signature,
    )


def _plan_text(
    *,
    conn: sqlite3.Connection,
    path: Path,
    watch_root: Path,
    full: bool,
    verbose: bool,
    log: Callable[[str], None],
) -> FilePlan:
    stored = _stored_path(path, watch_root)
    rel = _log_relative_path(path, watch_root)
    if not path.is_file():
        log(f"palace index: missing path={rel} reason=file-vanished")
        return _noop_plan(
            kind="text",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-vanished",
            full_delete=full,
        )
    prior = _prior_file_row(conn, watch_root=watch_root, stored=stored)
    prior_signature = None if full else _file_signature(prior)
    file_bytes = path.read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    if not full and prior is not None and prior[0] == file_hash:
        if verbose:
            log(f"palace index: noop path={rel} reason=file-hash-unchanged")
        return _noop_plan(
            kind="text",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="file-hash-unchanged",
            full_delete=False,
        )
    if looks_binary(file_bytes):
        log(f"palace index: skip kind=binary path={rel} reason=non-printable-bytes")
        return _noop_plan(
            kind="text",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="non-printable-bytes",
            full_delete=full,
        )
    chunks = tuple(parse_text(file_bytes=file_bytes))
    if not chunks:
        log(f"palace index: skip kind=text path={rel} reason=empty-body")
        return _noop_plan(
            kind="text",
            stored=stored,
            rel=rel,
            watch_root=watch_root,
            reason="empty-body",
            full_delete=full,
        )
    diff = _diff_sections(
        new_sections=chunks,
        prior_sections_json=None if full or prior is None else prior[1],
    )
    to_embed = (*diff.new, *diff.changed)
    chunk_ids = tuple(_standard_chunk_id(watch_root, stored, section) for section in to_embed)
    rows = tuple(
        PendingRow(
            chunk_id=chunk_id,
            section_index=section.section_index,
            window_index=section.window_index,
            heading=None,
            heading_depth=0,
            body=section.body,
            body_hash=section.body_hash,
            frontmatter_json=None,
        )
        for section, chunk_id in zip(to_embed, chunk_ids, strict=True)
    )
    return _ordinary_plan(
        kind="text",
        stored=stored,
        rel=rel,
        watch_root=watch_root,
        full=full,
        file_hash=file_hash,
        diff=diff,
        to_embed=to_embed,
        chunk_ids=chunk_ids,
        rows=rows,
        headings=tuple(None for _row in rows),
        prior_signature=prior_signature,
    )


def _ordinary_plan(
    *,
    kind: str,
    stored: str,
    rel: str,
    watch_root: Path,
    full: bool,
    file_hash: str,
    diff: _SectionDiff,
    to_embed: Sequence[_DiffableChunk],
    chunk_ids: tuple[str, ...],
    rows: tuple[PendingRow, ...],
    headings: tuple[str | None, ...],
    prior_signature: str | None,
) -> FilePlan:
    return FilePlan(
        kind=kind,
        stored=stored,
        rel=rel,
        watch_root=str(watch_root),
        noop=None,
        embed_inputs=tuple(
            scored_text(path=stored, heading=heading, body=row.body)
            for row, heading in zip(rows, headings, strict=True)
        ),
        chunk_ids=chunk_ids,
        rows=rows,
        stale_chunk_ids=_stale_chunk_ids(diff),
        file_row=PendingFileRow(
            path=stored,
            watch_root=str(watch_root),
            kind=kind,
            file_hash=file_hash,
            sections_json=_build_sections_json(
                unchanged=diff.unchanged,
                inserted=tuple(zip(to_embed, chunk_ids, strict=True)),
                prior_chunk_ids_by_position=diff.prior_chunk_ids_by_position,
            ),
        ),
        jsonl_offset=None,
        full_delete=full,
        counts=_counts(kind, diff),
        prior_signature=prior_signature,
    )


def egress_context_for(plan: FilePlan) -> EgressContext:
    """Return the provenance value a worker must re-establish."""
    return EgressContext(watch_root=plan.watch_root, chunk_ids=plan.chunk_ids)


def commit_plan(
    *,
    conn: sqlite3.Connection,
    plan: FilePlan,
    embeddings: Sequence[Sequence[float]],
    identity: EmbeddingIdentity | None,
    log: Callable[[str], None],
) -> IndexOutcome:
    """Commit one plan transactionally on the calling writer thread.

    The caller holds the store's writer lock. Inside the transaction the
    store's recorded identity must still equal ``identity`` (the embedder
    that produced ``embeddings``), and the plan's prior state must be the
    state the store holds now; otherwise nothing is written and
    :class:`StalePlanError` (or the identity refusal) is raised. A full
    rebuild passes ``identity=None``: it holds the lock for its whole run and
    replaces a stale or missing certificate at its end, so its commits are
    the remedy for a mismatch rather than subject to it.
    """
    if len(embeddings) != len(plan.rows):
        raise IndexError(
            f"embedder returned {len(embeddings)} vectors for {len(plan.rows)} planned rows"
        )
    if plan.noop is not None and not plan.full_delete:
        return plan.noop
    ingest_time = _now_iso()
    removed_by_delete = 0
    ensure_fts_rowid_map(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if identity is not None:
            assert_identity(conn, identity)
        if plan.prior_signature is not None:
            current = _current_signature(conn, plan)
            if current != plan.prior_signature:
                raise StalePlanError(f"{plan.rel} changed in the store since it was planned")
        if plan.full_delete:
            removed_by_delete = _delete_rows(
                conn=conn, watch_root=plan.watch_root, path_str=plan.stored
            )
        if plan.noop is None and plan.kind != "deleted":
            _delete_fts_chunks(conn, plan.stale_chunk_ids)
            for stale in plan.stale_chunk_ids:
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (stale,))
                conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (stale,))
            file_row = plan.file_row
            if file_row is None:
                raise IndexError(f"file plan for {plan.rel} has no files row")
            for row, vector in zip(plan.rows, embeddings, strict=True):
                conn.execute(
                    """
                    INSERT INTO chunks (
                        chunk_id, path, watch_root, kind,
                        section_index, window_index, heading, heading_depth,
                        body, body_hash, frontmatter_json, wikilinks_json, tags_json,
                        dataview_fields_json, embeds_json, ingest_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.chunk_id,
                        file_row.path,
                        file_row.watch_root,
                        file_row.kind,
                        row.section_index,
                        row.window_index,
                        row.heading,
                        row.heading_depth,
                        row.body,
                        row.body_hash,
                        row.frontmatter_json,
                        file_row.wikilinks_json,
                        file_row.tags_json,
                        file_row.dataview_fields_json,
                        file_row.embeds_json,
                        ingest_time,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (row.chunk_id, sqlite_vec.serialize_float32(vector)),
                )
                _insert_fts_chunk(conn, row.chunk_id, row.body)
            conn.execute(
                """
                INSERT INTO files (
                    path, watch_root, kind, file_hash, sections_json, last_indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(watch_root, path) DO UPDATE SET
                    kind = excluded.kind,
                    file_hash = excluded.file_hash,
                    sections_json = excluded.sections_json,
                    last_indexed_at = excluded.last_indexed_at
                """,
                (
                    file_row.path,
                    file_row.watch_root,
                    file_row.kind,
                    file_row.file_hash,
                    file_row.sections_json,
                    ingest_time,
                ),
            )
            if plan.jsonl_offset is not None:
                conn.execute(
                    """
                    INSERT INTO jsonl_cursors (watch_root, path, last_byte_offset, last_indexed_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(watch_root, path) DO UPDATE SET
                        last_byte_offset = excluded.last_byte_offset,
                        last_indexed_at = excluded.last_indexed_at
                    """,
                    (plan.watch_root, plan.stored, plan.jsonl_offset, ingest_time),
                )
        conn.execute("COMMIT")
    except Exception as error:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_error:
                error.add_note(f"Rollback failed: {rollback_error}")
        raise
    if plan.kind == "deleted":
        log(f"palace index: dropped path={plan.rel} chunks={removed_by_delete}")
        return IndexOutcome(kind="deleted", removed=removed_by_delete)
    if plan.noop is not None:
        return plan.noop
    outcome = plan.counts
    log(
        "palace index: indexed "
        f"path={plan.rel} kind={plan.kind} "
        f"new={outcome.new} changed={outcome.changed} "
        f"unchanged={outcome.unchanged} removed={outcome.removed}"
    )
    return outcome


def index_one(
    *,
    conn: sqlite3.Connection,
    embedder: Embedder,
    identity: EmbeddingIdentity,
    file_kind: str,
    change_kind: ChangeKind,
    path: Path,
    watch_root: Path,
    store: Path,
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
    verbose: bool = False,
    log: Callable[[str], None],
) -> IndexOutcome:
    """Plan and embed outside the writer lock, then lock only to commit.

    Used by the daemon and the per-file update. A plan the store or the file
    moved under is planned again against the current state, up to
    ``MAX_STALE_REPLANS`` times; a noop plan returns without taking the lock.
    Under the lock the file's existence must still agree with the plan: a
    file that reappeared behind a delete plan, or vanished behind an index
    plan, makes the plan stale. An identity mismatch is not stale; it is a
    terminal refusal for this embedder and propagates.
    """
    rel = _log_relative_path(path, watch_root)
    effective_kind = change_kind
    for attempt in range(1, MAX_STALE_REPLANS + 1):
        if attempt > 1:
            # Re-plan against disk reality: a file that reappeared behind a
            # delete plan is indexed, one that vanished behind an index plan
            # is dropped.
            effective_kind = "deleted" if not path.exists() else "created"
        plan = plan_one(
            conn=conn,
            file_kind=file_kind,
            change_kind=effective_kind,
            path=path,
            watch_root=watch_root,
            full=False,
            verbose=verbose,
            log=log,
        )
        if plan.noop is not None and not plan.full_delete:
            return plan.noop
        embeddings: list[list[float]] = []
        if plan.embed_inputs:
            context = egress_context_for(plan)
            with egress_context(watch_root=context.watch_root, chunk_ids=context.chunk_ids):
                embeddings = embedder.embed(plan.embed_inputs)
        with writer_lock(store, operation=f"index {rel}", timeout=lock_timeout):
            exists = path.exists()
            if (plan.kind == "deleted") == exists:
                state = "exists again" if exists else "vanished"
                log(
                    f"palace index: replan path={rel} reason=stale-plan attempt={attempt} "
                    f"({rel} {state} before its commit)"
                )
                continue
            try:
                return commit_plan(
                    conn=conn, plan=plan, embeddings=embeddings, identity=identity, log=log
                )
            except StalePlanError as exc:
                log(f"palace index: replan path={rel} reason=stale-plan attempt={attempt} ({exc})")
    raise IndexError(
        f"plan for {rel} went stale {MAX_STALE_REPLANS} times; another writer keeps changing it"
    )


def _insert_fts_chunk(
    conn: sqlite3.Connection, chunk_id: str, body: str, *, rowid: int | None = None
) -> None:
    """Insert one keyword row and its lookup row inside the caller's transaction.

    ``rowid`` keeps a copied chunk's keyword rowid when the store has not used it,
    so equal keyword scores order the same way as in the store it came from.
    """
    if (
        rowid is not None
        and conn.execute("SELECT 1 FROM chunks_fts_rows WHERE fts_rowid = ?", (rowid,)).fetchone()
        is None
    ):
        cursor = conn.execute(
            "INSERT INTO chunks_fts (rowid, chunk_id, body) VALUES (?, ?, ?)",
            (rowid, chunk_id, body),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO chunks_fts (chunk_id, body) VALUES (?, ?)", (chunk_id, body)
        )
    conn.execute(
        "INSERT INTO chunks_fts_rows (chunk_id, fts_rowid) VALUES (?, ?)",
        (chunk_id, cursor.lastrowid),
    )


def _delete_fts_chunks(conn: sqlite3.Connection, chunk_ids: Sequence[str]) -> None:
    """Delete these chunks' keyword rows through the keyword-row lookup.

    FTS5 cannot index equality on its UNINDEXED chunk_id column, so resolving
    ids against the keyword table would read every row; the lookup table maps
    each chunk to its rowid instead (Phase 18). There is no scan fallback: a
    store without a complete lookup is converted by ensure_fts_rowid_map before
    any write. The caller owns the transaction, including rollback across all
    indexes.
    """
    for start in range(0, len(chunk_ids), 512):
        batch = chunk_ids[start : start + 512]
        placeholders = ",".join("?" for _ in batch)
        rowids = conn.execute(
            f"SELECT fts_rowid FROM chunks_fts_rows WHERE chunk_id IN ({placeholders})",
            batch,
        ).fetchall()
        conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", rowids)
        conn.execute(f"DELETE FROM chunks_fts_rows WHERE chunk_id IN ({placeholders})", batch)


def _delete_rows(*, conn: sqlite3.Connection, watch_root: str, path_str: str) -> int:
    """Delete one file's rows inside the caller's transaction."""
    chunk_ids = [
        row[0]
        for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE watch_root = ? AND path = ?",
            (watch_root, path_str),
        ).fetchall()
    ]
    _delete_fts_chunks(conn, chunk_ids)
    for chunk_id in chunk_ids:
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))
    conn.execute("DELETE FROM chunks WHERE watch_root = ? AND path = ?", (watch_root, path_str))
    conn.execute("DELETE FROM files WHERE watch_root = ? AND path = ?", (watch_root, path_str))
    conn.execute(
        "DELETE FROM jsonl_cursors WHERE watch_root = ? AND path = ?", (watch_root, path_str)
    )
    return len(chunk_ids)


def delete_path(*, conn: sqlite3.Connection, watch_root: str, path_str: str) -> IndexOutcome:
    """Transactionally drop every row for one watch-root-relative path."""
    ensure_fts_rowid_map(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        removed = _delete_rows(conn=conn, watch_root=watch_root, path_str=path_str)
        conn.execute("COMMIT")
    except Exception as error:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_error:
                error.add_note(f"Rollback failed: {rollback_error}")
        raise
    return IndexOutcome(kind="deleted", removed=removed)


def _counts(kind: str, diff: _SectionDiff) -> IndexOutcome:
    return IndexOutcome(
        kind=kind,
        new=len(diff.new),
        changed=len(diff.changed),
        unchanged=len(diff.unchanged),
        removed=len(diff.removed),
        embedded=len(diff.new) + len(diff.changed),
    )


def _stale_chunk_ids(diff: _SectionDiff) -> tuple[str, ...]:
    stale = [chunk_id for _section, _window, chunk_id in diff.removed]
    for changed in diff.changed:
        prior = diff.prior_chunk_ids_by_position.get((changed.section_index, changed.window_index))
        if prior is not None:
            stale.append(prior)
    return tuple(stale)


def _standard_chunk_id(watch_root: Path, stored: str, section: _DiffableChunk) -> str:
    return compute_chunk_id(
        watch_root=str(watch_root),
        path=stored,
        section_index=section.section_index,
        window_index=section.window_index,
        body_hash=section.body_hash,
    )


def _build_sections_json(
    *,
    unchanged: Sequence[_DiffableChunk],
    inserted: Sequence[tuple[_DiffableChunk, str]],
    prior_chunk_ids_by_position: dict[tuple[int, int], str],
) -> str:
    records: list[tuple[int, int, str, str]] = []
    for section in unchanged:
        chunk_id = prior_chunk_ids_by_position[(section.section_index, section.window_index)]
        records.append((section.section_index, section.window_index, section.body_hash, chunk_id))
    for section, chunk_id in inserted:
        records.append((section.section_index, section.window_index, section.body_hash, chunk_id))
    records.sort(key=lambda item: (item[0], item[1]))
    return json.dumps([list(item) for item in records], separators=(",", ":"))


def _compute_jsonl_chunk_id(*, watch_root: str, path: str, chunk: JsonlChunk) -> str:
    if chunk.record_id is not None:
        # A globally unique record id is deliberately unsalted by root/path;
        # salting it would break later retraction by that same record id.
        return compute_record_id({"jsonl_record_id": chunk.record_id})
    return compute_chunk_id(
        watch_root=watch_root,
        path=path,
        section_index=chunk.section_index,
        window_index=chunk.window_index,
        body_hash=chunk.body_hash,
    )


def _count_prior_jsonl_records(prior_sections_json: str | None) -> int:
    if not prior_sections_json:
        return 0
    try:
        prior_list = json.loads(prior_sections_json)
    except json.JSONDecodeError:
        return 0
    seen = {int(entry[0]) for entry in prior_list if isinstance(entry, list) and len(entry) == 4}
    return max(seen) + 1 if seen else 0


def _synthesize_prior_chunks(prior_sections_json: str | None) -> tuple[JsonlChunk, ...]:
    if not prior_sections_json:
        return ()
    try:
        prior_list = json.loads(prior_sections_json)
    except json.JSONDecodeError:
        return ()
    return tuple(
        JsonlChunk(
            record_index=int(entry[0]),
            window_index=int(entry[1]),
            body="",
            body_hash=str(entry[2]),
            record_id=None,
            section_index=int(entry[0]),
        )
        for entry in prior_list
        if isinstance(entry, list) and len(entry) == 4
    )


def _diff_sections[DiffableT: _DiffableChunk](
    *, new_sections: Sequence[DiffableT], prior_sections_json: str | None
) -> _SectionDiff:
    prior_map: dict[tuple[int, int], tuple[str, str]] = {}
    if prior_sections_json:
        try:
            prior_list = json.loads(prior_sections_json)
        except json.JSONDecodeError:
            prior_list = []
        for entry in prior_list:
            if isinstance(entry, list) and len(entry) == 4:
                section_index, window_index, body_hash, chunk_id = entry
                prior_map[(int(section_index), int(window_index))] = (str(body_hash), str(chunk_id))
    unchanged: list[DiffableT] = []
    changed: list[DiffableT] = []
    new: list[DiffableT] = []
    for section in new_sections:
        prior = prior_map.get((section.section_index, section.window_index))
        if prior is None:
            new.append(section)
        elif prior[0] == section.body_hash:
            unchanged.append(section)
        else:
            changed.append(section)
    new_positions = {(item.section_index, item.window_index) for item in new_sections}
    removed = tuple(
        (section_index, window_index, chunk_id)
        for (section_index, window_index), (_body_hash, chunk_id) in prior_map.items()
        if (section_index, window_index) not in new_positions
    )
    return _SectionDiff(
        unchanged=tuple(unchanged),
        changed=tuple(changed),
        new=tuple(new),
        removed=removed,
        prior_chunk_ids_by_position={
            position: chunk_id for position, (_body_hash, chunk_id) in prior_map.items()
        },
    )
