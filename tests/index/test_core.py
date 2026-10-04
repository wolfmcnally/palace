"""Tests for the shared per-file index core (:mod:`palace.index.core`).

These exercise the core functions directly against a bootstrapped
``sqlite3.Connection`` + ``FakeEmbedder`` — the same logic the daemon's
:class:`palace.index.writer.WriterWorker` and the synchronous
:mod:`palace.index.build` walker both call. The headline
``test_writer_and_core_yield_identical_rows`` proves the writer queue
path and a direct core call agree row-for-row.
"""

from __future__ import annotations

import queue
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

from palace.index._errors import IndexError as PalaceIndexError
from palace.index.config import EMBED_DIM, prepare_chunks_db_path
from palace.index.core import (
    FilePlan,
    IndexOutcome,
    _delete_fts_chunks,
    _delete_rows,
    _insert_fts_chunk,
    _stored_path,
    commit_plan,
    delete_path,
    index_one,
    plan_one,
)
from palace.index.embedder_config import resolve_identity
from palace.index.enrich import scored_text
from palace.index.schema import init_db, verify_fts_rowid_map
from palace.index.writer import WriterWorker, _Sentinel, _WorkItem

from .conftest import FakeEmbedder


def _open(store: Path) -> sqlite3.Connection:
    """Open + bootstrap a chunks DB connection with sqlite-vec loaded."""
    conn = sqlite3.connect(str(prepare_chunks_db_path(store)), isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    init_db(conn, store=store)
    return conn


def _noop_log(_line: str) -> None:
    return None


def _store_of(conn: sqlite3.Connection) -> Path:
    """The store a ``_open`` connection was bootstrapped from."""
    return Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent.parent


def _index_kind(
    *,
    file_kind: str,
    conn: sqlite3.Connection,
    embedder: FakeEmbedder,
    path: Path,
    watch_root: Path,
    verbose: bool = False,
    log: Callable[[str], None] = _noop_log,
) -> IndexOutcome:
    store = _store_of(conn)
    return index_one(
        conn=conn,
        embedder=embedder,
        identity=resolve_identity(store),
        file_kind=file_kind,
        change_kind="created",
        path=path,
        watch_root=watch_root,
        store=store,
        verbose=verbose,
        log=log,
    )


def _index_markdown(**kwargs: Any) -> IndexOutcome:
    return _index_kind(file_kind="markdown", **kwargs)


def _index_jsonl(**kwargs: Any) -> IndexOutcome:
    return _index_kind(file_kind="jsonl", **kwargs)


def _index_code(**kwargs: Any) -> IndexOutcome:
    return _index_kind(file_kind="code", **kwargs)


def _index_text(**kwargs: Any) -> IndexOutcome:
    return _index_kind(file_kind="text", **kwargs)


def test_index_markdown_through_core_inserts_chunks(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "notes.md"
    note.write_text("## A\nbody A\n## B\nbody B\n## C\nbody C\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        outcome = _index_markdown(
            conn=conn,
            embedder=embedder,
            path=note,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        chunks_n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vec_n = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        ids_chunks = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks")}
        ids_vec = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks_vec")}
        ids_fts = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks_fts")}
    finally:
        conn.close()
    assert chunks_n == 3
    assert vec_n == 3
    assert fts_n == 3
    assert ids_chunks == ids_vec == ids_fts
    assert outcome == IndexOutcome(kind="markdown", new=3, embedded=3)


def test_markdown_embed_input_is_reconstructible_from_stored_columns(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "notes.md"
    note.write_text("## Decision\nbody bytes\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        path_str, heading, body = conn.execute(
            "SELECT path, heading, body FROM chunks WHERE kind = 'markdown'"
        ).fetchone()
    finally:
        conn.close()
    assert embedder.last_batch == [scored_text(path=path_str, heading=heading, body=body)]


def test_index_markdown_file_hash_short_circuit_is_noop(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "notes.md"
    note.write_text("## A\nbody A\n## B\nbody B\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        after_first = embedder.call_count
        outcome = _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
    finally:
        conn.close()
    assert outcome.noop is True
    assert outcome.noop_reason == "file-hash-unchanged"
    assert embedder.call_count == after_first


def test_index_markdown_single_section_change_reembeds_only_that_section(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "notes.md"
    note.write_text("## A\nbody A\n## B\nbody B\n## C\nbody C\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        before = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT section_index, chunk_id FROM chunks WHERE path = ?", ("notes.md",)
            )
        }
        note.write_text("## A\nbody A\n## B\nbody B CHANGED\n## C\nbody C\n", encoding="utf-8")
        outcome = _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        after = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT section_index, chunk_id FROM chunks WHERE path = ?", ("notes.md",)
            )
        }
    finally:
        conn.close()
    assert outcome == IndexOutcome(kind="markdown", changed=1, unchanged=2, embedded=1)
    assert after[0] == before[0]
    assert after[2] == before[2]
    assert after[1] != before[1]


def test_index_jsonl_through_core_append_only(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    log_path = tmp_watch_root / "events.jsonl"
    log_path.write_bytes(b'{"a":1}\n{"b":2}\n')
    conn = _open(tmp_store)
    try:
        _index_jsonl(
            conn=conn, embedder=embedder, path=log_path, watch_root=tmp_watch_root, log=_noop_log
        )
        before = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM chunks WHERE kind='jsonl' ORDER BY section_index"
            )
        ]
        with log_path.open("ab") as fh:
            fh.write(b'{"c":3}\n')
        outcome = _index_jsonl(
            conn=conn, embedder=embedder, path=log_path, watch_root=tmp_watch_root, log=_noop_log
        )
        after = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM chunks WHERE kind='jsonl' ORDER BY section_index"
            )
        ]
    finally:
        conn.close()
    assert len(before) == 2
    assert len(after) == 3
    assert after[:2] == before
    assert outcome.new == 1
    assert outcome.embedded == 1


def test_jsonl_embed_input_is_reconstructible_from_stored_columns(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    log_path = tmp_watch_root / "events.jsonl"
    log_path.write_bytes(b'{"a":1}\n{"b":2}\n')
    conn = _open(tmp_store)
    try:
        _index_jsonl(
            conn=conn, embedder=embedder, path=log_path, watch_root=tmp_watch_root, log=_noop_log
        )
        rows = conn.execute(
            "SELECT path, heading, body FROM chunks WHERE kind = 'jsonl' ORDER BY section_index"
        ).fetchall()
    finally:
        conn.close()
    assert embedder.last_batch == [
        scored_text(path=path_str, heading=heading, body=body) for path_str, heading, body in rows
    ]


def test_index_code_through_core(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    src = tmp_watch_root / "script.py"
    src.write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        outcome = _index_code(
            conn=conn, embedder=embedder, path=src, watch_root=tmp_watch_root, log=_noop_log
        )
        headings = [
            row[0]
            for row in conn.execute(
                "SELECT heading FROM chunks WHERE kind='code' ORDER BY section_index"
            )
        ]
    finally:
        conn.close()
    assert headings == ["alpha", "beta"]
    assert outcome.kind == "code"
    assert outcome.new == 2
    assert outcome.embedded == 2


def test_code_embed_input_is_reconstructible_from_stored_columns(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    src = tmp_watch_root / "sample.rs"
    src.write_text('fn alpha() {\n    println!("alpha");\n}\n', encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_code(
            conn=conn,
            embedder=embedder,
            path=src,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        path_str, heading, body = conn.execute(
            "SELECT path, heading, body FROM chunks WHERE kind = 'code'"
        ).fetchone()
    finally:
        conn.close()
    assert embedder.last_batch == [scored_text(path=path_str, heading=heading, body=body)]


def test_index_text_through_core(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "random.txt"
    note.write_text("a quick captured note\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        outcome = _index_text(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE kind='text'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert outcome.kind == "text"
    assert outcome.embedded == 1


def test_text_embed_input_is_reconstructible_from_stored_columns(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "random.txt"
    note.write_text("a quick captured note\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_text(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        path_str, heading, body = conn.execute(
            "SELECT path, heading, body FROM chunks WHERE kind = 'text'"
        ).fetchone()
    finally:
        conn.close()
    assert embedder.last_batch == [scored_text(path=path_str, heading=heading, body=body)]


def test_index_text_refuses_binary_bytes(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "garbage.txt"
    note.write_bytes(b"\x00" * 5000)
    logs: list[str] = []
    log: Callable[[str], None] = logs.append
    conn = _open(tmp_store)
    try:
        outcome = _index_text(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=log
        )
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    assert outcome.noop is True
    assert embedder.call_count == 0
    assert any("kind=binary" in line and "non-printable-bytes" in line for line in logs)


def test_delete_path_through_core_drops_all_tables(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    log_path = tmp_watch_root / "events.jsonl"
    log_path.write_bytes(b'{"a":1}\n{"b":2}\n')
    conn = _open(tmp_store)
    try:
        _index_jsonl(
            conn=conn, embedder=embedder, path=log_path, watch_root=tmp_watch_root, log=_noop_log
        )
        before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        outcome = delete_path(conn=conn, watch_root=str(tmp_watch_root), path_str="events.jsonl")
        counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE watch_root = ? AND path = ?",
                (str(tmp_watch_root), "events.jsonl"),
            ).fetchone()[0]
            for table in ("chunks", "files", "jsonl_cursors")
        }
        vec_n = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    finally:
        conn.close()
    assert before == 2
    assert outcome == IndexOutcome(kind="deleted", removed=2)
    assert counts == {"chunks": 0, "files": 0, "jsonl_cursors": 0}
    assert vec_n == 0
    assert fts_n == 0


def test_index_one_spurious_delete_guard(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    note = tmp_watch_root / "notes.md"
    note.write_text("## A\nbody A\n## B\nbody B\n", encoding="utf-8")
    logs: list[str] = []
    conn = _open(tmp_store)
    identity = resolve_identity(tmp_store)
    try:
        index_one(
            conn=conn,
            embedder=embedder,
            identity=identity,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=tmp_watch_root,
            store=tmp_store,
            log=logs.append,
        )
        before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        # The file still exists on disk — a ``deleted`` event must be a
        # no-op (the tempfile-rename guard).
        assert note.exists()
        outcome = index_one(
            conn=conn,
            embedder=embedder,
            identity=identity,
            file_kind="markdown",
            change_kind="deleted",
            path=note,
            watch_root=tmp_watch_root,
            store=tmp_store,
            log=logs.append,
        )
        after = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()
    assert before > 0
    assert after == before
    assert outcome.noop is True
    assert outcome.noop_reason == "path-still-exists"
    assert any("skip-delete" in line and "reason=path-still-exists" in line for line in logs)


def _run_writer(store: Path, watch_root: Path, note: Path, *, file_kind: str) -> None:
    """Drive one path through the public ``WriterWorker`` queue."""
    embedder = FakeEmbedder()
    work_queue: queue.Queue[_WorkItem | _Sentinel] = queue.Queue()
    completions: list[_WorkItem] = []
    worker = WriterWorker(
        store=store,
        embedder=embedder,
        identity=resolve_identity(store),
        work_queue=work_queue,
        on_completed=completions.append,
    )
    worker.start()
    try:
        work_queue.put(
            _WorkItem(
                change_kind="created",
                path=note,
                watch_root=watch_root,
                file_kind=file_kind,
            )
        )
        deadline = time.monotonic() + 2.0
        while not completions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()


def test_writer_and_core_yield_identical_relative_rows(
    tmp_path: Path,
    tmp_watch_root: Path,
) -> None:
    """The daemon's writer and a direct core call agree row-for-row.

    The compared ``path`` values are watch-root-relative (Phase 3.2);
    both modes store the same relative identity and the same chunk-ids.
    """
    body = "## A\nbody A\n## B\nbody B CHANGED\n## C\nbody C\n"
    note = tmp_watch_root / "notes.md"
    note.write_text(body, encoding="utf-8")

    store_writer = tmp_path / "store-writer"
    store_writer.mkdir()
    store_core = tmp_path / "store-core"
    store_core.mkdir()

    # DB-A: drive the public WriterWorker queue path.
    _run_writer(store_writer, tmp_watch_root, note, file_kind="markdown")

    # DB-B: a direct core call against a second store.
    conn = _open(store_core)
    try:
        _index_markdown(
            conn=conn,
            embedder=FakeEmbedder(),
            path=note,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
    finally:
        conn.close()

    select = (
        "SELECT chunk_id, path, body, body_hash, kind, section_index, window_index "
        "FROM chunks ORDER BY chunk_id"
    )
    conn_a = _open(store_writer)
    try:
        rows_a = list(conn_a.execute(select))
    finally:
        conn_a.close()
    conn_b = _open(store_core)
    try:
        rows_b = list(conn_b.execute(select))
    finally:
        conn_b.close()
    assert rows_a == rows_b
    assert len(rows_a) == 3
    # The stored ``path`` (column index 1) is watch-root-relative.
    assert {row[1] for row in rows_a} == {"notes.md"}


def test_core_stores_watch_root_relative_path(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    """``chunks.path`` / ``files.path`` are watch-root-relative; ``watch_root`` absolute."""
    notes_dir = tmp_watch_root / "notes"
    notes_dir.mkdir()
    note = notes_dir / "a.md"
    note.write_text("## A\nbody A\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        chunk_path = conn.execute("SELECT DISTINCT path FROM chunks").fetchone()[0]
        chunk_watch_root = conn.execute("SELECT DISTINCT watch_root FROM chunks").fetchone()[0]
        files_path = conn.execute("SELECT DISTINCT path FROM files").fetchone()[0]
    finally:
        conn.close()
    assert chunk_path == "notes/a.md"
    assert files_path == "notes/a.md"
    assert not chunk_path.startswith(("/", "~"))
    assert chunk_watch_root == str(tmp_watch_root)


def test_core_chunk_id_includes_watch_root(
    tmp_path: Path,
    embedder: FakeEmbedder,
) -> None:
    """Identical content under two roots → different ids; re-index → stable ids."""
    body = "## A\nbody A\n## B\nbody B\n"

    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.md").write_text(body, encoding="utf-8")
    (root_b / "a.md").write_text(body, encoding="utf-8")

    store_a = tmp_path / "store-a"
    store_a.mkdir()
    store_b = tmp_path / "store-b"
    store_b.mkdir()

    conn_a = _open(store_a)
    try:
        _index_markdown(
            conn=conn_a, embedder=embedder, path=root_a / "a.md", watch_root=root_a, log=_noop_log
        )
        ids_a = {row[0] for row in conn_a.execute("SELECT chunk_id FROM chunks")}
        # Re-index the same root: chunk-ids are stable (file-hash short-circuit
        # leaves them untouched, and a forced re-derivation would match).
        _index_markdown(
            conn=conn_a, embedder=embedder, path=root_a / "a.md", watch_root=root_a, log=_noop_log
        )
        ids_a_again = {row[0] for row in conn_a.execute("SELECT chunk_id FROM chunks")}
    finally:
        conn_a.close()

    conn_b = _open(store_b)
    try:
        _index_markdown(
            conn=conn_b, embedder=embedder, path=root_b / "a.md", watch_root=root_b, log=_noop_log
        )
        ids_b = {row[0] for row in conn_b.execute("SELECT chunk_id FROM chunks")}
    finally:
        conn_b.close()

    # (a) Identical relative path + bytes under two different absolute
    # roots → DIFFERENT chunk-ids (the watch_root salt).
    assert ids_a.isdisjoint(ids_b)
    # (b) Within one root, re-index → stable chunk-ids.
    assert ids_a == ids_a_again


def test_stored_path_refuses_path_outside_watch_root() -> None:
    """``_stored_path`` raises ``IndexError`` for a path outside the root."""
    with pytest.raises(PalaceIndexError) as exc:
        _stored_path(Path("/tmp/elsewhere/x.md"), Path("/tmp/root"))
    message = str(exc.value)
    assert "/tmp/elsewhere/x.md" in message
    assert "/tmp/root" in message


def test_delete_path_keys_on_relative_path(
    tmp_store: Path,
    tmp_watch_root: Path,
    embedder: FakeEmbedder,
) -> None:
    """Deleting by the relative key drops rows; deleting by the absolute is a no-op."""
    note = tmp_watch_root / "a.md"
    note.write_text("## A\nbody A\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=embedder, path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        # Deleting by the absolute path string is a no-op (the stored key
        # is relative).
        noop = delete_path(conn=conn, watch_root=str(tmp_watch_root), path_str=str(note))
        still_there = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        # Deleting by the relative key drops the rows.
        dropped = delete_path(conn=conn, watch_root=str(tmp_watch_root), path_str="a.md")
        gone = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()
    assert noop.removed == 0
    assert still_there > 0
    assert dropped.removed == still_there
    assert gone == 0


def test_plan_shapes_align_and_commit_rejects_vector_count_mismatch(
    tmp_store: Path, tmp_watch_root: Path
) -> None:
    note = tmp_watch_root / "plan.md"
    note.write_text("## A\nalpha\n## B\nbeta\n", encoding="utf-8")
    conn = _open(tmp_store)
    try:
        plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        assert isinstance(plan, FilePlan)
        assert len(plan.embed_inputs) == len(plan.rows) == len(plan.chunk_ids) == 2
        with pytest.raises(PalaceIndexError, match="1 vectors for 2 planned rows"):
            commit_plan(
                conn=conn,
                plan=plan,
                embeddings=[[0.0] * EMBED_DIM],
                identity=resolve_identity(tmp_store),
                log=_noop_log,
            )
    finally:
        conn.close()


def test_full_plan_bypasses_hash_short_circuit_and_resets_jsonl_cursor(
    tmp_store: Path, tmp_watch_root: Path
) -> None:
    note = tmp_watch_root / "note.md"
    note.write_text("## A\nalpha\n", encoding="utf-8")
    events = tmp_watch_root / "events.jsonl"
    events.write_text('{"body":"one"}\n', encoding="utf-8")
    conn = _open(tmp_store)
    try:
        _index_markdown(
            conn=conn, embedder=FakeEmbedder(), path=note, watch_root=tmp_watch_root, log=_noop_log
        )
        _index_jsonl(
            conn=conn,
            embedder=FakeEmbedder(),
            path=events,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        events.write_text('{"body":"one"}\n{"body":"two"}\n', encoding="utf-8")
        markdown_plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=tmp_watch_root,
            full=True,
            log=_noop_log,
        )
        jsonl_plan = plan_one(
            conn=conn,
            file_kind="jsonl",
            change_kind="created",
            path=events,
            watch_root=tmp_watch_root,
            full=True,
            log=_noop_log,
        )
        assert markdown_plan.full_delete is True
        assert len(markdown_plan.embed_inputs) == 1
        assert jsonl_plan.full_delete is True
        assert len(jsonl_plan.embed_inputs) == 2
        assert jsonl_plan.jsonl_offset == events.stat().st_size
    finally:
        conn.close()


def test_statement_delete_and_transactional_delete_drop_the_same_rows(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    note = root / "a.md"
    note.write_text("## A\nalpha\n", encoding="utf-8")
    snapshots: list[tuple[int, int, int]] = []
    for index in range(2):
        store = tmp_path / f"store-{index}"
        store.mkdir()
        conn = _open(store)
        try:
            _index_markdown(
                conn=conn, embedder=FakeEmbedder(), path=note, watch_root=root, log=_noop_log
            )
            if index == 0:
                delete_path(conn=conn, watch_root=str(root), path_str="a.md")
            else:
                conn.execute("BEGIN")
                assert _delete_rows(conn=conn, watch_root=str(root), path_str="a.md") == 1
                conn.execute("COMMIT")
            snapshots.append(
                (
                    int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                    int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
                    int(conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]),
                )
            )
        finally:
            conn.close()
    assert snapshots == [(0, 0, 0), (0, 0, 0)]

    # A replacement must not rescan the corpus once per stale chunk. Count
    # SQLite VM work rather than imposing a machine-dependent timing limit.
    store = tmp_path / "bulk-fts"
    store.mkdir()
    conn = _open(store)
    try:
        conn.execute("BEGIN")
        for index in range(5000):
            _insert_fts_chunk(conn, f"chunk-{index}", "synthetic searchable text")
        conn.execute("COMMIT")
        steps = 0

        def count_steps() -> int:
            nonlocal steps
            steps += 100
            return 0

        conn.execute("BEGIN")
        conn.set_progress_handler(count_steps, 100)
        _delete_fts_chunks(conn, [f"chunk-{index}" for index in range(1025)])
        conn.set_progress_handler(None, 0)
        assert steps < 1_000_000
        assert {r[0] for r in conn.execute("SELECT chunk_id FROM chunks_fts")} == {
            f"chunk-{index}" for index in range(1025, 5000)
        }
        verify_fts_rowid_map(conn)  # the lookup rows went with their keyword rows
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 5000
        verify_fts_rowid_map(conn)
    finally:
        conn.close()

    # Exercise failed commits as part of the same transaction-atomicity proof.
    # Each case owns its database so a failed rollback cannot contaminate another.
    for operation in ("index", "delete"):
        for failure_mode in ("automatic_rollback", "active", "rollback_failure"):
            case = tmp_path / f"{operation}-{failure_mode}"
            store, watch = case / "store", case / "watch"
            store.mkdir(parents=True)
            watch.mkdir()
            _assert_transaction_failure_preserves_primary_error(
                store, watch, operation, failure_mode
            )


def test_package_exports_match_core_surface() -> None:
    import palace.index as index_package

    for removed in ("index_markdown", "index_jsonl", "index_code", "index_text"):
        assert removed not in index_package.__all__
        assert not hasattr(index_package, removed)
    for added in ("FilePlan", "plan_one", "commit_plan"):
        assert added in index_package.__all__
        assert getattr(index_package, added) is not None
    for name in index_package.__all__:
        assert getattr(index_package, name) is not None


@pytest.fixture(autouse=True)
def _isolate_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests off the operator's real store."""
    monkeypatch.delenv("PALACE_STORE", raising=False)
    return None


def _assert_transaction_failure_preserves_primary_error(
    tmp_store: Path, tmp_watch_root: Path, operation: str, failure_mode: str
) -> None:
    primary = sqlite3.OperationalError("database or disk is full")

    class FailingConnection(sqlite3.Connection):
        armed = False
        rollback_calls = 0

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if self.armed and sql == "COMMIT":
                if failure_mode == "automatic_rollback":
                    super().execute("ROLLBACK")
                raise primary
            if self.armed and sql == "ROLLBACK":
                self.rollback_calls += 1
                if failure_mode == "rollback_failure":
                    raise sqlite3.OperationalError("rollback I/O failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(
        str(prepare_chunks_db_path(tmp_store)), isolation_level=None, factory=FailingConnection
    )
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        init_db(conn, store=tmp_store)
        note = tmp_watch_root / "failure.md"
        note.write_text("## A\noriginal\n", encoding="utf-8")
        original_plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        commit_plan(
            conn=conn,
            plan=original_plan,
            embeddings=[[0.0] * EMBED_DIM],
            identity=resolve_identity(tmp_store),
            log=_noop_log,
        )
        original_hash = conn.execute("SELECT file_hash FROM files").fetchone()[0]
        note.write_text("## A\nreplacement\n", encoding="utf-8")
        plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="modified",
            path=note,
            watch_root=tmp_watch_root,
            log=_noop_log,
        )
        conn.armed = True
        with pytest.raises(sqlite3.OperationalError) as caught:
            if operation == "index":
                commit_plan(
                    conn=conn,
                    plan=plan,
                    embeddings=[[1.0] * EMBED_DIM],
                    identity=resolve_identity(tmp_store),
                    log=_noop_log,
                )
            else:
                delete_path(conn=conn, watch_root=str(tmp_watch_root), path_str="failure.md")
        assert caught.value is primary
        assert conn.rollback_calls == (0 if failure_mode == "automatic_rollback" else 1)
        assert conn.in_transaction == (failure_mode == "rollback_failure")
        if failure_mode == "rollback_failure":
            assert primary.__notes__ == ["Rollback failed: rollback I/O failure"]
            conn.armed = False
            conn.execute("ROLLBACK")
        assert conn.execute("SELECT file_hash FROM files").fetchone()[0] == original_hash
        assert conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
