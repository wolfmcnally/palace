"""Exact document metadata over SQLite; no inference or filesystem classification.

The tables are an independently versioned, rebuildable component. Connections
supplied to the writer must be idle and use isolation_level=None. Readers may
share a caller's existing read transaction. Values are exact, case-sensitive
strings; the producer owns normalization and field meaning.

A batch write holds the store's writer lock (``palace.writer_lock``) for its
transaction, derived from the connection's database path, so it neither
interleaves with a whole-tree build's reconcile nor spoils that build's
certificate. The lock path is ``<store>/meta/index-writer.lock`` beside the
database at ``<store>/index/chunks.sqlite``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from palace.writer_lock import WRITER_LOCK_TIMEOUT_SECONDS, WriterLockTimeout, writer_lock

FORMAT = "document-metadata-v1"
MAX_PAGE = 1000


class MetadataError(ValueError):
    """Invalid metadata input, unavailable component or stale cursor."""


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: str
    fields: Mapping[str, Sequence[str]]
    watch_root: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class MetadataFilter:
    field: str
    any_of: Sequence[str] = ()
    all_of: Sequence[str] = ()


@dataclass(frozen=True)
class DocumentPage:
    documents: list[DocumentMetadata]
    generation: str
    cursor: str | None
    total: int | None = None


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise MetadataError(f"{label} must be a nonempty string without NUL")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise MetadataError(f"{label} must be valid UTF-8") from exc
    return value


def _strings(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise MetadataError(f"{label} must be a string array")
    return tuple(sorted({_string(value, label) for value in values}))


def normalize_filters(filters: Sequence[MetadataFilter]) -> tuple[MetadataFilter, ...]:
    if not isinstance(filters, (list, tuple)):
        raise MetadataError("filters must be an array")
    result = []
    for item in filters:
        if not isinstance(item, MetadataFilter):
            raise MetadataError("each filter must be a MetadataFilter")
        field = _string(item.field, "field")
        any_of = _strings(item.any_of, "any_of")
        all_of = _strings(item.all_of, "all_of")
        if bool(any_of) == bool(all_of):
            raise MetadataError("a filter requires exactly one nonempty any_of or all_of")
        result.append(MetadataFilter(field, any_of, all_of))
    # Stable query identity independent of input ordering and repeated predicates.
    return tuple(sorted(set(result), key=lambda item: (item.field, item.any_of, item.all_of)))


def parse_filters(value: object) -> tuple[MetadataFilter, ...]:
    """Validate the JSON representation used by CLI and portable consumers."""
    if not isinstance(value, list):
        raise MetadataError("filters must be an array")
    items = []
    for row in value:
        if not isinstance(row, dict) or set(row) not in ({"field", "any_of"}, {"field", "all_of"}):
            raise MetadataError("a filter requires field and exactly one of any_of/all_of")
        items.append(MetadataFilter(**row))
    return normalize_filters(items)


def _document(row: DocumentMetadata) -> DocumentMetadata:
    if not isinstance(row, DocumentMetadata) or not isinstance(row.fields, Mapping):
        raise MetadataError("invalid document metadata record")
    document_id = _string(row.document_id, "document_id")
    if (row.watch_root is None) != (row.path is None):
        raise MetadataError("watch_root and path must be supplied together")
    if row.watch_root is not None:
        root = _string(row.watch_root, "watch_root")
        path = _string(row.path, "path")
        if not PurePosixPath(root).is_absolute() or str(PurePosixPath(root)) != root:
            raise MetadataError("watch_root must be a canonical absolute POSIX path")
        if (
            PurePosixPath(path).is_absolute()
            or str(PurePosixPath(path)) != path
            or path == "."
            or ".." in PurePosixPath(path).parts
            or "\\" in path
        ):
            raise MetadataError("path must be a canonical relative POSIX path without '..'")
    fields = {}
    for key, values in row.fields.items():
        key = _string(key, "field")
        normalized = _strings(values, "field values")
        if normalized:
            fields[key] = normalized
    return DocumentMetadata(document_id, dict(sorted(fields.items())), row.watch_root, row.path)


_DDL = (
    "CREATE TABLE metadata_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE metadata_documents(document_id TEXT PRIMARY KEY COLLATE BINARY NOT NULL, "
    "watch_root TEXT, path TEXT, UNIQUE(watch_root,path), "
    "CHECK((watch_root IS NULL) = (path IS NULL)))",
    "CREATE TABLE metadata_values(field TEXT NOT NULL, value TEXT NOT NULL, "
    "document_id TEXT NOT NULL, PRIMARY KEY(field,value,document_id)) WITHOUT ROWID",
    "CREATE INDEX metadata_values_document ON metadata_values(document_id,field,value)",
)


def metadata_generation(conn: sqlite3.Connection, schema: str = "main") -> str:
    """Require a complete supported component, without creating or repairing it.

    ``schema`` names an attached database whose component is checked instead of
    the main one."""
    if not schema.isidentifier():
        raise MetadataError(f"invalid schema name {schema!r}")
    try:
        values = dict(conn.execute(f"SELECT key,value FROM {schema}.metadata_state"))
        if values.get("format") != FORMAT:
            raise MetadataError("metadata format unavailable or unsupported; rebuild metadata")
        generation = values.get("generation", "")
        if (
            not isinstance(generation, str)
            or len(generation) != 32
            or any(c not in "0123456789abcdef" for c in generation)
        ):
            raise MetadataError("invalid metadata generation; rebuild metadata")
        conn.execute(f"SELECT document_id FROM {schema}.metadata_documents LIMIT 0")
        conn.execute(f"SELECT field,value,document_id FROM {schema}.metadata_values LIMIT 0")
        return generation
    except sqlite3.Error as exc:
        raise MetadataError(f"metadata unavailable: {exc}") from exc


def _initialize(conn: sqlite3.Connection) -> None:
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('metadata_state','metadata_documents','metadata_values')"
    ).fetchall()
    if present:
        metadata_generation(conn)
        return
    for statement in _DDL:
        conn.execute(statement)
    conn.executemany(
        "INSERT INTO metadata_state VALUES (?,?)",
        [("format", FORMAT), ("generation", uuid.uuid4().hex)],
    )


def initialize_metadata(conn: sqlite3.Connection) -> None:
    """Initialize beside existing indexes without changing their data or identity."""
    if conn.in_transaction:
        raise MetadataError("metadata initialization requires an idle connection")
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('metadata_state','metadata_documents','metadata_values')"
    ).fetchone()
    if present is not None:
        metadata_generation(conn)
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        _initialize(conn)
        conn.execute("COMMIT")
    except (sqlite3.Error, MetadataError) as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MetadataError(f"metadata initialization unavailable: {exc}") from exc


def _hydrate(conn: sqlite3.Connection, ids: Sequence[str]) -> list[DocumentMetadata]:
    if not ids:
        return []
    slots = ",".join("?" for _ in ids)
    fields: dict[str, dict[str, list[str]]] = {key: {} for key in ids}
    for document_id, field, value in conn.execute(
        f"SELECT document_id,field,value FROM metadata_values WHERE document_id IN ({slots}) "
        "ORDER BY document_id,field,value",
        tuple(ids),
    ):
        fields[document_id].setdefault(field, []).append(value)
    records = {
        key: DocumentMetadata(key, fields[key], root, path)
        for key, root, path in conn.execute(
            f"SELECT document_id,watch_root,path FROM metadata_documents "
            f"WHERE document_id IN ({slots})",
            tuple(ids),
        )
    }
    return [records[key] for key in ids if key in records]


def _store_of(conn: sqlite3.Connection) -> Path | None:
    """Locate the store from the connection's main database path.

    An in-memory database has no other writer to coordinate with and returns
    ``None``; a file-backed one is ``<store>/index/chunks.sqlite`` in palace's
    layout, so the store is its grandparent directory.
    """
    for _sequence, name, file_path in conn.execute("PRAGMA database_list"):
        if name == "main":
            if isinstance(file_path, str) and file_path:
                return Path(file_path).resolve().parent.parent
            return None
    return None


def replace_documents(
    conn: sqlite3.Connection,
    documents: Sequence[DocumentMetadata],
    *,
    delete_ids: Sequence[str] = (),
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
) -> str:
    """Replace complete records and delete explicit IDs in one owned transaction.

    Unknown deletions and identical replacements are no-ops. Caller-owned
    connections retain their busy timeout. A conflict rolls back the whole batch.
    The store's writer lock is held for the transaction; a holder that does not
    release it within ``lock_timeout`` seconds is named in the refusal.
    """
    if conn.in_transaction or conn.isolation_level is not None:
        raise MetadataError("metadata writes require an idle autocommit connection")
    if not isinstance(documents, (list, tuple)) or not isinstance(delete_ids, (list, tuple)):
        raise MetadataError("documents and delete_ids must be arrays")
    records = [_document(row) for row in documents]
    removals = [_string(key, "delete_id") for key in delete_ids]
    ids = [row.document_id for row in records] + removals
    if len(set(ids)) != len(ids):
        raise MetadataError("duplicate document operation in metadata batch")
    associations = [(row.watch_root, row.path) for row in records if row.watch_root is not None]
    if len(set(associations)) != len(associations):
        raise MetadataError("duplicate file association in metadata batch")
    try:
        store = _store_of(conn)
    except sqlite3.Error as exc:
        raise MetadataError(f"metadata batch refused: {exc}") from exc
    if store is None:
        return _replace_documents_locked(conn, records, removals)
    try:
        with writer_lock(store, operation="metadata", timeout=lock_timeout):
            return _replace_documents_locked(conn, records, removals)
    except WriterLockTimeout as exc:
        raise MetadataError(f"metadata batch refused: {exc}") from exc


def _apply_documents(
    conn: sqlite3.Connection, records: Sequence[DocumentMetadata], removals: Sequence[str]
) -> bool:
    """Replace complete records and delete explicit ids inside the caller's transaction.

    Records are compared with the stored state before anything is deleted, so an
    identical batch changes nothing; the generation advances once, only when a
    row actually changed. Returns whether anything changed.
    """
    changed = []
    for row in records:
        existing = _hydrate(conn, [row.document_id])
        if not existing or _document(existing[0]) != row:
            changed.append(row)
    deletes = [row.document_id for row in changed] + list(removals)
    modified = bool(changed)
    for key in deletes:
        conn.execute("DELETE FROM metadata_values WHERE document_id=?", (key,))
        deleted = conn.execute("DELETE FROM metadata_documents WHERE document_id=?", (key,))
        modified = modified or deleted.rowcount > 0
    for row in changed:
        conn.execute(
            "INSERT INTO metadata_documents VALUES (?,?,?)",
            (row.document_id, row.watch_root, row.path),
        )
        conn.executemany(
            "INSERT INTO metadata_values VALUES (?,?,?)",
            [
                (field, value, row.document_id)
                for field, values in row.fields.items()
                for value in values
            ],
        )
    if modified:
        conn.execute(
            "UPDATE metadata_state SET value=? WHERE key='generation'", (uuid.uuid4().hex,)
        )
    return modified


def _replace_documents_locked(
    conn: sqlite3.Connection, records: list[DocumentMetadata], removals: list[str]
) -> str:
    begun = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        begun = True
        _initialize(conn)
        _apply_documents(conn, records, removals)
        generation = metadata_generation(conn)
        conn.execute("COMMIT")
        return generation
    except (sqlite3.Error, MetadataError) as exc:
        if begun:
            conn.execute("ROLLBACK")
        raise MetadataError(f"metadata batch refused: {exc}") from exc


def matching_documents(
    filters: Sequence[MetadataFilter], *, after: str | None = None
) -> tuple[str, tuple[str, ...]]:
    """Compile value-index scans and SQL intersections; interpolate no caller text."""
    normalized = normalize_filters(filters)
    clauses, params = [], []
    for predicate in normalized:
        values = predicate.any_of or predicate.all_of
        if predicate.any_of:
            slots = ",".join("?" for _ in values)
            clause = f"SELECT document_id FROM metadata_values WHERE field=? AND value IN ({slots})"
            args = [predicate.field, *values]
            if after is not None:
                clause += " AND document_id>? COLLATE BINARY"
                args.append(after)
            clauses.append(clause)
            params.extend(args)
        else:
            for value in values:
                clause = "SELECT document_id FROM metadata_values WHERE field=? AND value=?"
                args = [predicate.field, value]
                if after is not None:
                    clause += " AND document_id>? COLLATE BINARY"
                    args.append(after)
                clauses.append(clause)
                params.extend(args)
    if not clauses:
        clause = "SELECT document_id FROM metadata_documents"
        if after is not None:
            clause += " WHERE document_id>? COLLATE BINARY"
            params.append(after)
        clauses.append(clause)
    return " INTERSECT ".join(clauses), tuple(params)


def matching_chunks(filters: Sequence[MetadataFilter]) -> tuple[str, tuple[str, ...]]:
    sql, parameters = matching_documents(filters)
    return (
        "SELECT c.chunk_id FROM metadata_documents d JOIN chunks c "
        "ON c.watch_root=d.watch_root AND c.path=d.path "
        f"WHERE d.document_id IN ({sql})",
        parameters,
    )


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    owned = not conn.in_transaction
    try:
        if owned:
            conn.execute("BEGIN")
        yield
    finally:
        if owned and conn.in_transaction:
            conn.execute("ROLLBACK")


def lookup_documents(
    conn: sqlite3.Connection,
    filters: Sequence[MetadataFilter] = (),
    *,
    limit: int = 50,
    cursor: str | None = None,
    count: bool = False,
) -> DocumentPage:
    """Look up a bounded page using exact indexed predicates, never inference."""
    normalized = normalize_filters(filters)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise MetadataError(f"limit must be between 1 and {MAX_PAGE}")
    if not isinstance(count, bool):
        raise MetadataError("count must be boolean")
    identity = hashlib.sha256(
        json.dumps(
            [FORMAT, [(item.field, item.any_of, item.all_of) for item in normalized]],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        with _snapshot(conn):
            generation = metadata_generation(conn)
            after = None
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor) > 16384:
                        raise ValueError("invalid cursor")
                    payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                    if (
                        not isinstance(payload, list)
                        or len(payload) != 4
                        or payload[:3] != [FORMAT, identity, generation]
                    ):
                        raise ValueError("cursor query or generation mismatch")
                    after = _string(payload[3], "cursor document ID")
                except (ValueError, TypeError, UnicodeError) as exc:
                    raise MetadataError("invalid or stale metadata cursor") from exc
            sql, args = matching_documents(normalized, after=after)
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT document_id FROM ({sql}) "
                    "ORDER BY document_id COLLATE BINARY LIMIT ?",
                    (*args, limit + 1),
                )
            ]
            next_cursor = None
            if len(ids) > limit:
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        [FORMAT, identity, generation, ids[limit - 1]], separators=(",", ":")
                    ).encode()
                ).decode()
            total = None
            if count:
                whole, params = matching_documents(normalized)
                total = conn.execute(
                    f"SELECT COUNT(DISTINCT document_id) FROM ({whole})", params
                ).fetchone()[0]
            return DocumentPage(_hydrate(conn, ids[:limit]), generation, next_cursor, total)
    except sqlite3.Error as exc:
        raise MetadataError(f"metadata lookup unavailable: {exc}") from exc


@contextmanager
def metadata_connection(db_path: Path, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
    """Open only the metadata component; no embedding configuration is needed."""
    if writable:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        uri = db_path.resolve().as_uri() + ("?mode=rwc" if writable else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    except sqlite3.Error as exc:
        raise MetadataError(f"metadata database unavailable: {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()
