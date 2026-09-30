"""sqlite schema bootstrap + record dataclasses + chunk-id helper.

This module is the single source of truth for:

- The six-table chunks DB schema at ``<store>/index/chunks.sqlite``, including
  ``index_meta`` and its self-describing embed-text convention marker.
- The ``ChunkRecord`` and ``FileRecord`` frozen dataclasses the writer
  hands to the SQL layer.
- The :func:`compute_chunk_id` helper that delegates to
  :func:`palace.daemons.capture.ids.compute_record_id` — there is no
  second SHA-256 implementation in palace. A chunk's id is the SHA-256
  hex of the canonical JSON form of ``{watch_root, path, section_index,
  window_index, body_hash}`` — the ``watch_root`` salt makes the id
  cross-root-unique now that ``path`` is watch-root-relative (the same
  relative ``path`` under two different roots yields distinct ids).
- The ``files`` and ``jsonl_cursors`` tables carry a composite
  ``PRIMARY KEY (watch_root, path)``: ``path`` is the watch-root-relative
  POSIX string and ``watch_root`` is the absolute root, so two roots may
  carry the same relative ``path`` without colliding. ``jsonl_cursors``
  carries its own ``watch_root`` column for the same reason.

The schema-version marker at ``<store>/meta/schema-version`` carries the
literal string ``"chunks-v1"``. A genuinely empty chunks DB is stamped with
the complete embedding identity; a populated partial or mismatched DB remains
stale until a certified ``palace index build --full`` completes.
:func:`init_db` is idempotent — a second call against an existing DB only
re-asserts the schema-version match.
A mismatch raises :class:`palace.index._errors.IndexError` with a
single-line message naming the rebuild step (no migration shims; the
greenfield-until-released policy holds).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite_vec

from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError
from palace.index.config import (
    EMBED_CONVENTION,
    EMBED_DIM,
    SCHEMA_TAG,
    EmbeddingIdentity,
    chunks_db_path,
    schema_version_path,
)
from palace.index.embedder_config import resolve_identity
from palace.metadata import MetadataError, initialize_metadata

__all__ = [
    "ChunkRecord",
    "FileRecord",
    "RecordedIdentity",
    "assert_identity",
    "assert_identity_readable",
    "assert_schema_version",
    "compute_chunk_id",
    "identity_divergences",
    "identity_mismatch_line",
    "init_db",
    "read_identity",
    "read_data_version",
    "readable_divergences",
    "stamp_identity",
]


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One row of the canonical ``chunks`` table.

    Wikilinks, tags, Dataview fields, and embeds are JSON-encoded
    once per chunk (per the phase's "per-chunk, not per-file" choice).
    Open Question in the phase: whether to deduplicate frontmatter via a
    ``files_frontmatter`` JSON column. Default per-chunk for now.
    """

    chunk_id: str
    path: str
    watch_root: str
    kind: str
    section_index: int
    window_index: int
    heading: str | None
    heading_depth: int
    body: str
    body_hash: str
    frontmatter_json: str | None
    wikilinks_json: str
    tags_json: str
    dataview_fields_json: str
    embeds_json: str
    ingest_time: str


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One row of the canonical ``files`` table.

    ``sections_json`` carries the prior parse's ``(section_index,
    window_index, body_hash, chunk_id)`` tuples so the writer's diff
    pass can identify unchanged sections without re-embedding them.
    """

    path: str
    watch_root: str
    kind: str
    file_hash: str
    sections_json: str
    last_indexed_at: str


@dataclass(frozen=True, slots=True)
class RecordedIdentity:
    """The four identity rows read back from ``index_meta`` as text."""

    convention: str | None
    model: str | None
    provider: str | None
    dim: str | None


def compute_chunk_id(
    *,
    watch_root: str,
    path: str,
    section_index: int,
    window_index: int,
    body_hash: str,
) -> str:
    """Compute the canonical chunk id for one chunk.

    The canonical body is ``{watch_root, path, section_index,
    window_index, body_hash}``. ``path`` is the watch-root-relative POSIX
    string; ``watch_root`` is the absolute root. The ``watch_root`` salt
    is load-bearing for cross-root uniqueness: now that ``path`` is
    relative, two roots can carry the same relative ``path`` (e.g.
    ``README.md``), and without the salt their chunks would collide on a
    single ``chunk_id``. Salting on ``watch_root`` keeps each root's
    chunks distinct while leaving the id prefix-independent within one
    root (the relative ``path`` does not change when the tree relocates).

    Delegates to :func:`palace.daemons.capture.ids.compute_record_id` —
    palace ships exactly one SHA-256 idiom and this helper inherits it.
    The dict does not carry an ``id`` field, so the canonical-JSON
    helper's guard does not fire.
    """
    body: dict[str, Any] = {
        "watch_root": watch_root,
        "path": path,
        "section_index": section_index,
        "window_index": window_index,
        "body_hash": body_hash,
    }
    return compute_record_id(body)


CHUNKS_ROOT_PATH_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_root_path ON chunks(watch_root, path)"
)


def _ddl_statements() -> tuple[str, ...]:
    """The schema DDL, run once on bootstrap."""
    return (
        # Canonical row store. Every promoted fact about a chunk lives
        # here; the two virtual tables below are derived indexes.
        """
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id          TEXT PRIMARY KEY,
            path              TEXT NOT NULL,
            watch_root        TEXT NOT NULL,
            kind              TEXT NOT NULL,
            section_index     INTEGER NOT NULL,
            window_index      INTEGER NOT NULL,
            heading           TEXT,
            heading_depth     INTEGER NOT NULL,
            body              TEXT NOT NULL,
            body_hash         TEXT NOT NULL,
            frontmatter_json  TEXT,
            wikilinks_json    TEXT NOT NULL,
            tags_json         TEXT NOT NULL,
            dataview_fields_json TEXT NOT NULL,
            embeds_json       TEXT NOT NULL,
            ingest_time       TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_watch_root ON chunks(watch_root)",
        # One file's chunks by (watch_root, path) (Phase 18): with only the two single-column
        # indexes the planner may choose watch_root and read every chunk under that root.
        CHUNKS_ROOT_PATH_INDEX,
        # ``path`` is the watch-root-relative POSIX string and
        # ``watch_root`` the absolute root; the composite primary key lets
        # two roots carry the same relative ``path`` without colliding.
        """
        CREATE TABLE IF NOT EXISTS files (
            path            TEXT NOT NULL,
            watch_root      TEXT NOT NULL,
            kind            TEXT NOT NULL,
            file_hash       TEXT NOT NULL,
            sections_json   TEXT NOT NULL,
            last_indexed_at TEXT NOT NULL,
            PRIMARY KEY (watch_root, path)
        )
        """,
        # Per-path byte-offset cursor for the JSONL pipeline. Phase 2.4
        # additive within ``chunks-v1``: append-only JSONL streams are
        # tailed forward (the writer reads ``last_byte_offset`` and
        # never re-parses earlier bytes). Dropped alongside the chunks
        # rows on a ``deleted`` event so a future file at the same path
        # starts clean. ``path`` is watch-root-relative and ``watch_root``
        # absolute; the composite primary key keeps two roots' same-named
        # cursors distinct.
        """
        CREATE TABLE IF NOT EXISTS jsonl_cursors (
            path             TEXT NOT NULL,
            watch_root       TEXT NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_indexed_at  TEXT NOT NULL,
            PRIMARY KEY (watch_root, path)
        )
        """,
        # Embed-text convention marker. Additive within ``chunks-v1``;
        # keeping it inside the DB makes it travel with portable artifacts.
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        # Keyword-row lookup (Phase 18). FTS5 cannot index its UNINDEXED
        # ``chunk_id`` column, so finding one chunk's keyword row would scan the
        # whole keyword table; this derived table maps each chunk to its FTS
        # rowid. Additive within ``chunks-v1``; written with every keyword
        # insert and filled once for an older store by ensure_fts_rowid_map.
        """
        CREATE TABLE IF NOT EXISTS chunks_fts_rows (
            chunk_id  TEXT PRIMARY KEY,
            fts_rowid INTEGER NOT NULL UNIQUE
        )
        """,
        # sqlite-vec virtual table. ``FLOAT[N]`` declares a fixed-dim
        # f32 vector column; sqlite_vec.serialize_float32 is the writer
        # idiom (a bytes blob, not a list).
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        )
        """,
        # FTS5 virtual table. ``chunk_id UNINDEXED`` lets the writer
        # store the id alongside the body without paying the tokenization
        # cost on that column.
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            body,
            tokenize = "porter unicode61 remove_diacritics 2"
        )
        """,
    )


def _check_schema_version(store: Path) -> None:
    """Read or write ``<store>/meta/schema-version``; raise on mismatch."""
    path = schema_version_path(store)
    if path.exists():
        recorded = path.read_text(encoding="utf-8").strip()
        if recorded != SCHEMA_TAG:
            raise IndexError(
                f"index schema mismatch: expected {SCHEMA_TAG}, found {recorded}; "
                "rebuild the chunks DB"
            )
        return
    path.write_text(SCHEMA_TAG, encoding="utf-8")


def assert_schema_version(store: Path) -> None:
    """Require an existing, matching ``<store>/meta/schema-version``; never write it.

    A per-file update refuses a store the build has not created rather than
    creating one, so the marker's absence names the full build that fixes it.
    """
    path = store / "meta" / "schema-version"
    if not path.is_file():
        raise IndexError(
            f"no schema-version marker at {path}; build the store first with "
            "'palace index build --full'"
        )
    recorded = path.read_text(encoding="utf-8").strip()
    if recorded != SCHEMA_TAG:
        raise IndexError(
            f"index schema mismatch: expected {SCHEMA_TAG}, found {recorded}; rebuild the chunks DB"
        )


def read_identity(conn: sqlite3.Connection) -> RecordedIdentity:
    """Read the complete identity in one query; absence stays explicit."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'index_meta'"
    ).fetchone()
    if table_exists is None:
        return RecordedIdentity(None, None, None, None)
    values = {
        str(key): str(value)
        for key, value in conn.execute(
            """
            SELECT key, value FROM index_meta
            WHERE key IN ('embed_convention', 'embed_model', 'embed_provider', 'embed_dim')
            """
        ).fetchall()
    }
    return RecordedIdentity(
        convention=values.get("embed_convention"),
        model=values.get("embed_model"),
        provider=values.get("embed_provider"),
        dim=values.get("embed_dim"),
    )


def stamp_identity(conn: sqlite3.Connection, identity: EmbeddingIdentity) -> None:
    """Upsert all four axes; the caller owns the transaction."""
    rows = (
        ("embed_convention", identity.convention),
        ("embed_model", identity.model),
        ("embed_provider", identity.provider),
        ("embed_dim", str(identity.dim)),
    )
    conn.executemany(
        """
        INSERT INTO index_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        rows,
    )


def identity_divergences(
    *, found: RecordedIdentity, expected: EmbeddingIdentity
) -> tuple[str, ...]:
    """Return every write-time divergence in stable axis order."""
    pairs = (
        ("convention", found.convention, expected.convention),
        ("model", found.model, expected.model),
        ("provider", found.provider, expected.provider),
        ("dim", found.dim, str(expected.dim)),
    )
    return tuple(
        f"{axis} found='{actual if actual is not None else 'none'}' expected='{wanted}'"
        for axis, actual, wanted in pairs
        if actual != wanted
    )


def readable_divergences(found: RecordedIdentity) -> tuple[str, ...]:
    """Bind reader compatibility while permitting a foreign model/provider value."""
    divergences: list[str] = []
    if found.convention != EMBED_CONVENTION:
        value = found.convention if found.convention is not None else "none"
        divergences.append(f"convention found='{value}' expected='{EMBED_CONVENTION}'")
    if found.model is None:
        divergences.append("model not recorded")
    if found.provider is None:
        divergences.append("provider not recorded")
    expected_dim = str(EMBED_DIM)
    if found.dim != expected_dim:
        value = found.dim if found.dim is not None else "none"
        divergences.append(f"dim found='{value}' expected='{expected_dim}'")
    return tuple(divergences)


def identity_mismatch_line(divergences: tuple[str, ...]) -> str:
    """Render the one-line refusal shared by writers and vector readers."""
    return (
        "embedding-identity mismatch: "
        + "; ".join(divergences)
        + " — rebuild it with 'palace index build --full'"
    )


def assert_identity(conn: sqlite3.Connection, expected: EmbeddingIdentity) -> None:
    """Refuse a write unless every recorded axis matches the store config."""
    divergences = identity_divergences(found=read_identity(conn), expected=expected)
    if divergences:
        raise IndexError(identity_mismatch_line(divergences))


def assert_identity_readable(conn: sqlite3.Connection) -> None:
    """Refuse vector reads on incompatible or convention-only stores."""
    divergences = readable_divergences(read_identity(conn))
    if divergences:
        raise IndexError(identity_mismatch_line(divergences))


def read_data_version(conn: sqlite3.Connection) -> int:
    """Read this connection's writer-independent SQLite change counter.

    Values are meaningful only when compared across time on this same
    connection. Commits by another connection move the value; commits by this
    connection do not.
    """
    row = conn.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise IndexError("SQLite returned no PRAGMA data_version row")
    return int(row[0])


# Columns palace's own DDL guarantees on each table it owns as a real table.
# A pre-existing table of the same name missing any of these was written by
# something other than palace, and the ``CREATE TABLE IF NOT EXISTS`` DDL is a
# silent no-op against it — the failure would otherwise surface several
# statements later as a raw ``sqlite3.OperationalError: no such column``.
_REQUIRED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chunks", ("chunk_id", "path", "watch_root", "kind", "body", "body_hash")),
    ("files", ("path", "watch_root", "kind", "file_hash", "sections_json")),
    ("jsonl_cursors", ("path", "watch_root", "last_byte_offset")),
    ("index_meta", ("key", "value")),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table``, or an empty set if it is absent."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _assert_not_foreign_schema(conn: sqlite3.Connection, *, store: Path) -> None:
    """Refuse a chunks DB at this path that some other tool wrote.

    A store path can legitimately already hold a ``chunks.sqlite`` written by a
    different indexer — an external consumer pointing palace at a directory it
    already uses for its own database. Palace's ``CREATE TABLE IF NOT EXISTS``
    statements do not adopt or migrate such a table; they no-op, and the first
    statement that names a palace column dies with a raw
    ``sqlite3.OperationalError``. Detect it up front and say what to do.
    """
    for table, required in _REQUIRED_COLUMNS:
        found = _table_columns(conn, table)
        if not found:
            continue  # absent — palace's DDL will create it
        missing = [column for column in required if column not in found]
        if not missing:
            continue
        raise IndexError(
            f"{chunks_db_path(store)} holds a '{table}' table that palace did not write "
            f"(missing {', '.join(missing)}; found {', '.join(sorted(found))}) — palace will "
            "not adopt or migrate a foreign schema. Move that database aside, or point "
            "--store at a directory palace owns."
        )


FTS_ROWID_MAP_KEY = "fts_rowid_map"


def verify_fts_rowid_map(conn: sqlite3.Connection) -> None:
    """Refuse unless the keyword-row lookup and the keyword table agree row for row."""
    missing = conn.execute(
        "SELECT count(*) FROM chunks_fts AS f LEFT JOIN chunks_fts_rows AS r "
        "ON r.fts_rowid = f.rowid AND r.chunk_id = f.chunk_id WHERE r.fts_rowid IS NULL"
    ).fetchone()[0]
    extra = conn.execute(
        "SELECT count(*) FROM chunks_fts_rows AS r LEFT JOIN chunks_fts AS f "
        "ON f.rowid = r.fts_rowid WHERE f.rowid IS NULL"
    ).fetchone()[0]
    if missing or extra:
        raise IndexError(
            f"keyword-row lookup disagrees with the keyword table ({missing} keyword rows "
            f"without a lookup row, {extra} lookup rows without a keyword row); rebuild the "
            "store with 'palace index build --full'"
        )


def ensure_fts_rowid_map(conn: sqlite3.Connection, *, in_transaction: bool = False) -> None:
    """Give the store its keyword-row lookup, once. The caller holds the writer lock.

    With ``in_transaction`` the caller owns an open immediate transaction and the
    conversion joins it, so a later refusal or failure rolls the conversion back
    with everything else.

    In steady state this is one indexed read of ``index_meta``. A store written
    before the lookup existed is converted in one immediate transaction: the
    table is created if absent, filled from the keyword table in one pass,
    checked, and marked complete; any failure rolls back and leaves it unmarked,
    so the next writer converts it.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts_rows'"
    ).fetchone()
    if (
        table is not None
        and conn.execute(
            "SELECT 1 FROM index_meta WHERE key = ? AND value = 'complete'", (FTS_ROWID_MAP_KEY,)
        ).fetchone()
    ):
        return
    if not in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks_fts_rows ("
            "chunk_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL UNIQUE)"
        )
        conn.execute(CHUNKS_ROOT_PATH_INDEX)
        if not conn.execute(
            "SELECT 1 FROM index_meta WHERE key = ? AND value = 'complete'", (FTS_ROWID_MAP_KEY,)
        ).fetchone():
            conn.execute("DELETE FROM chunks_fts_rows")
            conn.execute(
                "INSERT INTO chunks_fts_rows (chunk_id, fts_rowid) "
                "SELECT chunk_id, rowid FROM chunks_fts"
            )
            verify_fts_rowid_map(conn)
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, 'complete')",
                (FTS_ROWID_MAP_KEY,),
            )
        if not in_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if not in_transaction:
            conn.execute("ROLLBACK")
        raise


def init_db(conn: sqlite3.Connection, *, store: Path) -> None:
    """Bootstrap the chunks DB schema on ``conn``.

    Loads the sqlite-vec extension, runs the ``CREATE … IF NOT
    EXISTS`` statements, sets ``PRAGMA journal_mode=WAL`` and
    ``synchronous=NORMAL``, then reads or writes
    ``<store>/meta/schema-version``. Idempotent — repeated calls against
    the same store are safe. The independent metadata component is initialized
    without rewriting chunks; an invalid component refuses bootstrap until its
    documented metadata-only recovery is performed. (This is the property the writer thread
    relies on when it re-runs ``init_db`` after the server's one-shot
    bootstrap connection has closed).
    """
    # Order matters: enable the extension loader before sqlite_vec.load.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    _assert_not_foreign_schema(conn, store=store)

    for statement in _ddl_statements():
        conn.execute(statement)
    chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    meta_count = int(conn.execute("SELECT COUNT(*) FROM index_meta").fetchone()[0])
    if chunk_count == 0 and meta_count == 0:
        stamp_identity(conn, resolve_identity(store))
        # A new store keeps its keyword-row lookup from its first write.
        conn.execute(
            "INSERT OR IGNORE INTO index_meta (key, value) VALUES (?, 'complete')",
            (FTS_ROWID_MAP_KEY,),
        )
    conn.commit()

    try:
        initialize_metadata(conn)
    except MetadataError as exc:
        raise IndexError(str(exc)) from exc
    _check_schema_version(store)
