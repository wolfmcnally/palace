"""Opt-in per-store change feed of committed vector writes (Phase 35).

A store that an operator has enabled (``palace index vector-feed enable``) holds
two extra tables and two triggers; a store that never enabled the feed holds
none of them, and no writer adds a row or object to it.

- ``chunks_vec_feed`` records every actual insert into and delete from
  ``chunks_vec``, in the writer's own transaction, with the inserted bytes.
  ``seq`` only grows (``AUTOINCREMENT``), so a reader replays the entries after
  the position it has applied, up to the position its own snapshot holds. A
  delete clears the payloads of earlier inserts of the same chunk in the same
  transaction, so the feed never retains a removed vector past the commit that
  removed it.
- ``chunks_vec_feed_state`` is one row: the lineage ``epoch``; ``floor_seq``
  (entries at or below it are pruned) and ``last_seq``; ``vector_rows``, the
  live count of ``chunks_vec``; and four monotonic totals. The triggers count
  every insert into and delete from ``chunks`` by any writer of any version
  (``chunk_inserts``, ``chunk_deletes``); feed-aware writers count the same
  rows they write (``feed_chunk_inserts``, ``feed_chunk_deletes``). Unequal
  totals mean some writer changed ``chunks`` without the feed: the feed is
  ``diverged`` and a reader refuses it. A replace of one chunk by one chunk
  moves both trigger totals and neither feed total, so it cannot cancel out.
  A raw rewrite of ``chunks_vec`` alone, outside every supported writer, is
  not detectable.

Every figure here is read from the state row: O(1), and it carries no chunk
id, text or vector.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

from palace.index._errors import IndexError
from palace.index.config import VECTOR_FEED_TAIL_ROWS

__all__ = [
    "FEED_STATE_TABLE",
    "FEED_TABLE",
    "VectorFeedDiverged",
    "VectorFeedState",
    "disable_vector_feed",
    "enable_vector_feed",
    "prune_vector_feed",
    "read_vector_feed_state",
    "record_feed_chunk_rows",
    "record_vector_delete",
    "record_vector_insert",
    "reset_vector_feed_lineage",
    "vector_feed_enabled",
]

FEED_TABLE = "chunks_vec_feed"
FEED_STATE_TABLE = "chunks_vec_feed_state"
_INSERT_TRIGGER = "chunks_vec_feed_chunk_insert"
_DELETE_TRIGGER = "chunks_vec_feed_chunk_delete"
_FEED_INDEX = "chunks_vec_feed_chunk"

_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {FEED_TABLE} (
        seq       INTEGER PRIMARY KEY AUTOINCREMENT,
        op        TEXT NOT NULL CHECK (op IN ('insert', 'delete')),
        chunk_id  TEXT NOT NULL,
        embedding BLOB
    )
    """,
    f"CREATE INDEX IF NOT EXISTS {_FEED_INDEX} ON {FEED_TABLE} (chunk_id)",
    f"""
    CREATE TABLE IF NOT EXISTS {FEED_STATE_TABLE} (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        epoch              TEXT NOT NULL,
        floor_seq          INTEGER NOT NULL,
        last_seq           INTEGER NOT NULL,
        vector_rows        INTEGER NOT NULL,
        chunk_inserts      INTEGER NOT NULL,
        chunk_deletes      INTEGER NOT NULL,
        feed_chunk_inserts INTEGER NOT NULL,
        feed_chunk_deletes INTEGER NOT NULL
    )
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {_INSERT_TRIGGER} AFTER INSERT ON chunks BEGIN
        UPDATE {FEED_STATE_TABLE} SET chunk_inserts = chunk_inserts + 1;
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {_DELETE_TRIGGER} AFTER DELETE ON chunks BEGIN
        UPDATE {FEED_STATE_TABLE} SET chunk_deletes = chunk_deletes + 1;
    END
    """,
)

_STATE_COLUMNS = (
    "epoch, floor_seq, last_seq, vector_rows, "
    "chunk_inserts, chunk_deletes, feed_chunk_inserts, feed_chunk_deletes"
)


class VectorFeedDiverged(IndexError):
    """A writer changed ``chunks`` without recording the change in the feed.

    This is an integrity refusal: a cache built from the feed could serve a stale
    answer, so nothing may fall back around it. Re-running
    ``palace index vector-feed enable`` starts a new lineage from the store as it is.
    """


@dataclass(frozen=True, slots=True)
class VectorFeedState:
    """One read of an enabled store's feed state row; every figure is O(1)."""

    epoch: str
    floor_seq: int
    last_seq: int
    vector_rows: int
    chunk_inserts: int
    chunk_deletes: int
    feed_chunk_inserts: int
    feed_chunk_deletes: int

    @property
    def live_chunks(self) -> int:
        return self.chunk_inserts - self.chunk_deletes

    @property
    def chunks_without_vector(self) -> int:
        return self.live_chunks - self.vector_rows

    @property
    def feed_entries(self) -> int:
        return self.last_seq - self.floor_seq

    @property
    def diverged(self) -> bool:
        return (
            self.chunk_inserts != self.feed_chunk_inserts
            or self.chunk_deletes != self.feed_chunk_deletes
        )

    def check(self) -> VectorFeedState:
        """Return this state, or raise :class:`VectorFeedDiverged`."""
        if self.diverged:
            raise VectorFeedDiverged(
                "vector feed diverged: a writer changed chunks without recording it "
                f"(chunk inserts {self.chunk_inserts} vs feed {self.feed_chunk_inserts}, "
                f"chunk deletes {self.chunk_deletes} vs feed {self.feed_chunk_deletes}); "
                "re-run 'palace index vector-feed enable' to start a new lineage"
            )
        return self


def vector_feed_enabled(conn: sqlite3.Connection, schema: str = "main") -> bool:
    """Whether ``schema`` holds the feed; one read of the schema catalog."""
    return (
        conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
            (FEED_STATE_TABLE,),
        ).fetchone()
        is not None
    )


def read_vector_feed_state(
    conn: sqlite3.Connection, schema: str = "main"
) -> VectorFeedState | None:
    """Read the state row, or ``None`` when the feed is not enabled.

    A state table without exactly one well-formed row is an integrity error.
    """
    if not vector_feed_enabled(conn, schema):
        return None
    rows = conn.execute(f"SELECT {_STATE_COLUMNS} FROM {schema}.{FEED_STATE_TABLE}").fetchall()
    if len(rows) != 1:
        raise IndexError(f"vector feed state is malformed: {len(rows)} state rows")
    row = rows[0]
    if not isinstance(row[0], str) or not row[0]:
        raise IndexError("vector feed state is malformed: no lineage epoch")
    counts = row[1:]
    if any(not isinstance(value, int) or value < 0 for value in counts):
        raise IndexError("vector feed state is malformed: a count is not a non-negative integer")
    state = VectorFeedState(row[0], *counts)
    if state.floor_seq > state.last_seq or state.live_chunks < 0 or state.vector_rows < 0:
        raise IndexError("vector feed state is malformed: inconsistent positions or counts")
    return state


def record_vector_insert(conn: sqlite3.Connection, chunk_id: str, embedding: bytes) -> None:
    """Record one committed-to-be insert into ``chunks_vec``, with its bytes."""
    cursor = conn.execute(
        f"INSERT INTO main.{FEED_TABLE} (op, chunk_id, embedding) VALUES ('insert', ?, ?)",
        (chunk_id, embedding),
    )
    conn.execute(
        f"UPDATE main.{FEED_STATE_TABLE} SET last_seq = ?, vector_rows = vector_rows + 1",
        (cursor.lastrowid,),
    )


def record_vector_delete(conn: sqlite3.Connection, chunk_id: str) -> None:
    """Record one delete from ``chunks_vec`` and clear the chunk's earlier payloads."""
    conn.execute(
        f"UPDATE main.{FEED_TABLE} SET embedding = NULL "
        "WHERE chunk_id = ? AND op = 'insert' AND embedding IS NOT NULL",
        (chunk_id,),
    )
    cursor = conn.execute(
        f"INSERT INTO main.{FEED_TABLE} (op, chunk_id, embedding) VALUES ('delete', ?, NULL)",
        (chunk_id,),
    )
    conn.execute(
        f"UPDATE main.{FEED_STATE_TABLE} SET last_seq = ?, vector_rows = vector_rows - 1",
        (cursor.lastrowid,),
    )


def record_feed_chunk_rows(
    conn: sqlite3.Connection, *, inserted: int = 0, deleted: int = 0
) -> None:
    """Add the ``chunks`` rows a feed-aware writer inserted or deleted to the feed totals."""
    if inserted or deleted:
        conn.execute(
            f"UPDATE main.{FEED_STATE_TABLE} SET "
            "feed_chunk_inserts = feed_chunk_inserts + ?, "
            "feed_chunk_deletes = feed_chunk_deletes + ?",
            (inserted, deleted),
        )


def prune_vector_feed(conn: sqlite3.Connection) -> None:
    """Delete, by sequence range, only the entries beyond :data:`VECTOR_FEED_TAIL_ROWS`."""
    row = conn.execute(f"SELECT floor_seq, last_seq FROM main.{FEED_STATE_TABLE}").fetchone()
    floor_seq, last_seq = int(row[0]), int(row[1])
    new_floor = last_seq - VECTOR_FEED_TAIL_ROWS
    if new_floor <= floor_seq:
        return
    conn.execute(
        f"DELETE FROM main.{FEED_TABLE} WHERE seq > ? AND seq <= ?", (floor_seq, new_floor)
    )
    conn.execute(f"UPDATE main.{FEED_STATE_TABLE} SET floor_seq = ?", (new_floor,))


def reset_vector_feed_lineage(conn: sqlite3.Connection) -> None:
    """Start a new lineage inside the caller's transaction: new epoch, recount, empty feed.

    The recount reads every row of ``chunks`` and ``chunks_vec`` once; it runs only when
    an operator enables the feed and when a full rebuild certifies the store.
    """
    conn.execute(f"DELETE FROM main.{FEED_TABLE}")
    sequence = conn.execute(
        "SELECT seq FROM main.sqlite_sequence WHERE name = ?", (FEED_TABLE,)
    ).fetchone()
    position = 0 if sequence is None else int(sequence[0])
    chunks = int(conn.execute("SELECT count(*) FROM main.chunks").fetchone()[0])
    vectors = int(conn.execute("SELECT count(*) FROM main.chunks_vec").fetchone()[0])
    conn.execute(f"DELETE FROM main.{FEED_STATE_TABLE}")
    conn.execute(
        f"INSERT INTO main.{FEED_STATE_TABLE} (id, {_STATE_COLUMNS}) "
        "VALUES (1, ?, ?, ?, ?, ?, 0, ?, 0)",
        (secrets.token_hex(16), position, position, vectors, chunks, chunks),
    )


def enable_vector_feed(conn: sqlite3.Connection) -> VectorFeedState:
    """Create the feed objects if absent and start a new lineage, in one transaction.

    The caller holds the store's writer lock. Re-running it on an enabled store resets
    the lineage, which is how an operator clears a divergence.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _DDL:
            conn.execute(statement)
        reset_vector_feed_lineage(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    state = read_vector_feed_state(conn)
    assert state is not None
    return state


def disable_vector_feed(conn: sqlite3.Connection) -> bool:
    """Drop every feed object in one transaction; return whether the feed was enabled."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        enabled = vector_feed_enabled(conn)
        conn.execute(f"DROP TRIGGER IF EXISTS main.{_INSERT_TRIGGER}")
        conn.execute(f"DROP TRIGGER IF EXISTS main.{_DELETE_TRIGGER}")
        conn.execute(f"DROP TABLE IF EXISTS main.{FEED_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS main.{FEED_STATE_TABLE}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return enabled
