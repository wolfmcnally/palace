"""Per-file store operations: copy named files between stores and remove named files.

``copy_paths`` moves one or more files' rows (files, chunks, vectors, keyword
rows, JSONL cursors and document metadata) from a source store into a
destination store without embedding anything, replacing whatever the
destination held for those files. ``remove_paths`` deletes every row of the
named files. Both run in one transaction under the writer lock, and both find
keyword rows through the keyword-row lookup, so neither reads another file's
keyword rows.

A copy holds both stores' writer locks, taken in the order of their resolved
lock paths, so no rebuild is mid-reconcile in the source while it is read and
two copies in opposite directions cannot deadlock. Both stores must record the
same complete embedder identity, the source must index the given watch root,
and the destination may hold no other root; otherwise the copy is refused
before anything is written.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import sqlite_vec

from palace.index._errors import IndexError
from palace.index.config import WRITER_BUSY_TIMEOUT_SECONDS, chunks_db_path
from palace.index.core import _delete_rows, _insert_fts_chunk
from palace.index.schema import (
    RecordedIdentity,
    assert_schema_version,
    ensure_fts_rowid_map,
    identity_mismatch_line,
    readable_divergences,
)
from palace.metadata import (
    DocumentMetadata,
    MetadataError,
    _apply_documents,
    _document,
    metadata_generation,
)
from palace.writer_lock import WRITER_LOCK_TIMEOUT_SECONDS, writer_lock, writer_lock_path

__all__ = ["StoreOpResult", "copy_paths", "remove_paths"]


@dataclass(frozen=True)
class StoreOpResult:
    """What one call changed: files written or removed, and whether metadata changed."""

    copied: int
    removed: int
    metadata_changed: bool


def _relative(paths: Sequence[str | Path], root: Path) -> list[str]:
    """Watch-root-relative POSIX paths; an absolute path must lie under the root."""
    out: list[str] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise IndexError(f"path {raw} is not under watch_root {root}") from exc
        else:
            text = PurePosixPath(str(raw)).as_posix()
        if text in ("", ".") or text.startswith("/") or ".." in PurePosixPath(text).parts:
            raise IndexError(f"path {raw} is not a file under watch_root {root}")
        if text not in out:
            out.append(text)
    if not out:
        raise IndexError("name at least one path")
    return out


def _open(store: Path) -> sqlite3.Connection:
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        raise IndexError(f"no index at {db_path}; build it first with 'palace index build --full'")
    assert_schema_version(store)
    conn = sqlite3.connect(
        db_path.resolve().as_uri(),
        uri=True,
        isolation_level=None,
        timeout=WRITER_BUSY_TIMEOUT_SECONDS,
    )
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@contextmanager
def _locked(stores: Sequence[Path], operation: str, timeout: float) -> Iterator[None]:
    if not math.isfinite(timeout) or timeout < 0:
        raise IndexError("lock_timeout must be a finite non-negative number of seconds")
    with ExitStack() as stack:
        for store in sorted(stores, key=lambda item: str(writer_lock_path(item).resolve())):
            stack.enter_context(writer_lock(store, operation=operation, timeout=timeout))
        yield


def _identity(conn: sqlite3.Connection, schema: str) -> RecordedIdentity:
    values = {
        str(key): str(value)
        for key, value in conn.execute(
            f"SELECT key, value FROM {schema}.index_meta WHERE key IN "
            "('embed_convention', 'embed_model', 'embed_provider', 'embed_dim')"
        )
    }
    return RecordedIdentity(
        convention=values.get("embed_convention"),
        model=values.get("embed_model"),
        provider=values.get("embed_provider"),
        dim=values.get("embed_dim"),
    )


def _documents_at(
    conn: sqlite3.Connection, schema: str, root: str, paths: Sequence[str]
) -> list[DocumentMetadata]:
    """The complete metadata documents associated with these files in one schema."""
    slots = ",".join("?" for _ in paths)
    rows = conn.execute(
        f"SELECT document_id, path FROM {schema}.metadata_documents "
        f"WHERE watch_root = ? AND path IN ({slots})",
        (root, *paths),
    ).fetchall()
    documents = []
    for document_id, path in rows:
        fields: dict[str, list[str]] = {}
        for field, value in conn.execute(
            f"SELECT field, value FROM {schema}.metadata_values WHERE document_id = ? "
            "ORDER BY field, value",
            (document_id,),
        ):
            fields.setdefault(field, []).append(value)
        documents.append(_document(DocumentMetadata(document_id, fields, root, path)))
    return documents


def remove_paths(
    *,
    store: Path,
    watch_root: Path,
    paths: Sequence[str | Path],
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
) -> StoreOpResult:
    """Delete every row of the named files, in one transaction under the writer lock."""
    root = watch_root.expanduser().resolve()
    targets = _relative(paths, root)
    with _locked([store], "remove", lock_timeout):
        conn = _open(store)
        try:
            removed = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Conversion, checks and writes share one transaction: a failure leaves nothing.
                ensure_fts_rowid_map(conn, in_transaction=True)
                _metadata_valid(conn, "main")
                removals = [
                    doc.document_id for doc in _documents_at(conn, "main", str(root), targets)
                ]
                for rel in targets:
                    had_file = conn.execute(
                        "SELECT 1 FROM files WHERE watch_root = ? AND path = ?", (str(root), rel)
                    ).fetchone()
                    _delete_rows(conn=conn, watch_root=str(root), path_str=rel)
                    removed += had_file is not None
                changed = _apply_documents(conn, [], removals)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    return StoreOpResult(copied=0, removed=removed, metadata_changed=changed)


def copy_paths(
    *,
    source: Path,
    destination: Path,
    watch_root: Path,
    paths: Sequence[str | Path],
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
) -> StoreOpResult:
    """Copy the named files' rows from ``source`` into ``destination`` without embedding.

    Each named file's destination rows are replaced by the source's; a file the
    source does not hold is removed from the destination. Chunk ids and vector
    bytes are preserved; keyword rows are written afresh from the chunk bodies.
    """
    root = watch_root.expanduser().resolve()
    targets = sorted(_relative(paths, root))
    if chunks_db_path(source).resolve() == chunks_db_path(destination).resolve():
        raise IndexError("source and destination are the same store; refused")
    with _locked([source, destination], "copy", lock_timeout):
        # The source is converted too, under the lock this copy already holds: an older store
        # gains the keyword-row lookup and the chunks(watch_root, path) index, so the copy reads
        # one file's rows without touching the rest and keeps the source's keyword order.
        with closing(_open(source)) as source_conn:
            ensure_fts_rowid_map(source_conn)
        conn = _open(destination)
        try:
            conn.execute(
                "ATTACH DATABASE ? AS source",
                (chunks_db_path(source).resolve().as_uri() + "?mode=ro",),
            )
            return _copy_locked(conn, str(root), targets)
        finally:
            conn.close()


def _metadata_valid(conn: sqlite3.Connection, schema: str) -> None:
    try:
        metadata_generation(conn, schema)
    except MetadataError as exc:
        raise IndexError(f"{'destination' if schema == 'main' else 'source'} store: {exc}") from exc


def _copy_locked(conn: sqlite3.Connection, root: str, targets: Sequence[str]) -> StoreOpResult:
    """Checks, conversion and writes in one immediate transaction on the destination."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _copy_in_transaction(conn, root, targets)
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _source_rowid(conn: sqlite3.Connection, chunk_id: str) -> int | None:
    """The chunk's keyword rowid in the source (converted before the copy began)."""
    row = conn.execute(
        "SELECT fts_rowid FROM source.chunks_fts_rows WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    return None if row is None else int(row[0])


def _copy_in_transaction(
    conn: sqlite3.Connection, root: str, targets: Sequence[str]
) -> StoreOpResult:
    source_identity, destination_identity = _identity(conn, "source"), _identity(conn, "main")
    for label, identity in (("source", source_identity), ("destination", destination_identity)):
        divergences = readable_divergences(identity)
        if divergences:
            raise IndexError(f"{label} store: " + identity_mismatch_line(divergences))
    if source_identity != destination_identity:
        raise IndexError(
            "source and destination record different embedder identities; a copy would mix "
            "vectors from different embedders — refused"
        )
    if (
        conn.execute("SELECT 1 FROM source.files WHERE watch_root = ? LIMIT 1", (root,)).fetchone()
        is None
    ):
        raise IndexError(f"the source store does not index watch root {root}; refused")
    other = conn.execute(
        "SELECT watch_root FROM main.files WHERE watch_root <> ? LIMIT 1", (root,)
    ).fetchone()
    if other is not None:
        raise IndexError(f"the destination store also indexes watch root {other[0]}; refused")
    _metadata_valid(conn, "source")
    _metadata_valid(conn, "main")
    present = [
        rel
        for rel in targets
        if conn.execute(
            "SELECT 1 FROM source.files WHERE watch_root = ? AND path = ?", (root, rel)
        ).fetchone()
    ]
    # The metadata batch is computed against the destination as it stands, before any write;
    # a file the source no longer holds brings no metadata and loses the destination's.
    incoming = _documents_at(conn, "source", root, present) if present else []
    incoming_ids = {doc.document_id for doc in incoming}
    for doc in incoming:
        held = conn.execute(
            "SELECT watch_root, path FROM main.metadata_documents WHERE document_id = ?",
            (doc.document_id,),
        ).fetchone()
        if held is not None and (held[0] != root or held[1] not in targets):
            raise IndexError(
                f"document id {doc.document_id} belongs to another file in the destination "
                f"({held[1]}); refused"
            )
    removals = [
        doc.document_id
        for doc in _documents_at(conn, "main", root, targets)
        if doc.document_id not in incoming_ids
    ]
    ensure_fts_rowid_map(conn, in_transaction=True)
    copied = removed = 0
    for rel in targets:
        key = (root, rel)
        held_here = conn.execute(
            "SELECT 1 FROM main.files WHERE watch_root = ? AND path = ?", key
        ).fetchone()
        _delete_rows(conn=conn, watch_root=root, path_str=rel)
        if rel not in present:
            removed += held_here is not None
            continue
        conn.execute(
            "INSERT INTO main.files SELECT * FROM source.files WHERE watch_root = ? AND path = ?",
            key,
        )
        conn.execute(
            "INSERT INTO main.chunks SELECT * FROM source.chunks WHERE watch_root = ? AND path = ?",
            key,
        )
        for chunk_id, body in conn.execute(
            "SELECT chunk_id, body FROM source.chunks WHERE watch_root = ? AND path = ? "
            "ORDER BY rowid",
            key,
        ).fetchall():
            conn.execute(
                "INSERT INTO main.chunks_vec (chunk_id, embedding) "
                "SELECT chunk_id, embedding FROM source.chunks_vec WHERE chunk_id = ?",
                (chunk_id,),
            )
            _insert_fts_chunk(conn, chunk_id, body, rowid=_source_rowid(conn, chunk_id))
        conn.execute(
            "INSERT INTO main.jsonl_cursors SELECT * FROM source.jsonl_cursors "
            "WHERE watch_root = ? AND path = ?",
            key,
        )
        copied += 1
    changed = _apply_documents(conn, incoming, removals)
    return StoreOpResult(copied=copied, removed=removed, metadata_changed=changed)
