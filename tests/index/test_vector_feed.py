"""Proofs of the opt-in per-store vector change feed (Phase 35).

A store that never enabled the feed must be untouched by every writer; an enabled store's
feed must carry exactly the committed vector changes with their bytes, clear removed
payloads, prune by range, keep its lineage, detect a writer that bypassed it, and report
every figure in constant work.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

import palace.index.vector_feed as feed_module
from palace.index.build import build
from palace.index.cli import dispatch_index
from palace.index.config import chunks_db_path
from palace.index.schema import init_db
from palace.index.store_ops import _open, copy_paths, remove_paths
from palace.index.vector_feed import (
    FEED_STATE_TABLE,
    FEED_TABLE,
    VectorFeedDiverged,
    VectorFeedState,
    enable_vector_feed,
    prune_vector_feed,
    read_vector_feed_state,
    record_feed_chunk_rows,
    record_vector_delete,
    record_vector_insert,
)
from palace.watch.config import WatchRootsConfig, default_config_path

from .conftest import FakeEmbedder

# palace.index re-exports the build function under the submodule's name.
build_module = importlib.import_module("palace.index.build")

NOTES = {
    "a.md": "# Alpha\n\nalpha orchard lantern notes\n\n## Second\n\nalpha second section\n",
    "b.md": "# Beta\n\nbeta harbor lantern notes\n",
    "c.md": "# Gamma\n\ngamma quarry notes\n",
}
_HELPERS = ("record_vector_insert", "record_vector_delete", "record_feed_chunk_rows")


def _connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(chunks_db_path(store)), isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _configured(tmp_path: Path, name: str, root: Path) -> Path:
    store = tmp_path / name
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    return store


def _watch(tmp_path: Path, name: str = "watch") -> Path:
    root = tmp_path / name
    root.mkdir()
    for file_name, body in NOTES.items():
        (root / file_name).write_text(body, encoding="utf-8")
    return root


def _build(store: Path, root: Path, *, full: bool = False) -> None:
    build(store=store, watch_root_filter=root, full=full, embedder=FakeEmbedder())


def _empty(store: Path) -> None:
    chunks_db_path(store).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(store)) as conn:
        init_db(conn, store=store)


def _enable(store: Path) -> VectorFeedState:
    with closing(_open(store)) as conn:
        return enable_vector_feed(conn)


def _state(store: Path) -> VectorFeedState:
    with closing(_connect(store)) as conn:
        state = read_vector_feed_state(conn)
    assert state is not None
    return state


def _vectors(store: Path) -> dict[str, bytes]:
    with closing(_connect(store)) as conn:
        return {
            str(chunk_id): bytes(embedding)
            for chunk_id, embedding in conn.execute("SELECT chunk_id, embedding FROM chunks_vec")
        }


def _feed(store: Path, after: int = 0) -> list[tuple[int, str, str, bytes | None]]:
    with closing(_connect(store)) as conn:
        return [
            (int(seq), str(op), str(chunk_id), None if payload is None else bytes(payload))
            for seq, op, chunk_id, payload in conn.execute(
                f"SELECT seq, op, chunk_id, embedding FROM {FEED_TABLE} WHERE seq > ? ORDER BY seq",
                (after,),
            )
        ]


def _rows(store: Path) -> dict[str, Any]:
    """Every table a writer touches, ingestion timestamps dropped, plus the schema."""
    with closing(_connect(store)) as conn:

        def table(name: str, order: str, drop: str | None = None) -> list[Any]:
            columns = [
                row[1] for row in conn.execute(f"PRAGMA table_info({name})") if row[1] != drop
            ]
            return conn.execute(
                f"SELECT {', '.join(columns)} FROM {name} ORDER BY {order}"
            ).fetchall()

        return {
            "files": table("files", "path", "last_indexed_at"),
            "chunks": table("chunks", "chunk_id", "ingest_time"),
            "vectors": conn.execute(
                "SELECT chunk_id, embedding FROM chunks_vec ORDER BY chunk_id"
            ).fetchall(),
            "keyword": conn.execute(
                "SELECT chunk_id, body FROM chunks_fts ORDER BY chunk_id"
            ).fetchall(),
            "schema": conn.execute(
                "SELECT type, name FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
        }


def _writer_matrix(
    tmp_path: Path, tag: str, *, enable: Callable[[Path, Path], None]
) -> Iterator[tuple[str, Path]]:
    """Add, replace, delete, remove and copy; yields (step, store) after each write."""
    root = _watch(tmp_path, f"{tag}-watch")
    source = _configured(tmp_path, f"{tag}-source", root)
    destination = _configured(tmp_path, f"{tag}-destination", root)
    _empty(source)
    _empty(destination)
    enable(source, destination)
    _build(source, root, full=True)
    yield "build", source
    (root / "a.md").write_text("# Alpha\n\nalpha rewritten lantern\n", encoding="utf-8")
    (root / "d.md").write_text("# Delta\n\ndelta added notes\n", encoding="utf-8")
    (root / "b.md").unlink()
    _build(source, root)
    yield "update", source
    remove_paths(store=source, watch_root=root, paths=["c.md"])
    yield "remove", source
    copy_paths(source=source, destination=destination, watch_root=root, paths=["a.md", "d.md"])
    yield "copy", destination
    copy_paths(source=source, destination=destination, watch_root=root, paths=["a.md", "c.md"])
    yield "copy-replace", destination


def test_never_enabled_store_is_unaffected_by_every_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a feed helper ran for a store without the feed")

    # Writers reach the feed helpers through the feed module itself, imported on first use.
    for name in (*_HELPERS, "prune_vector_feed", "reset_vector_feed_lineage"):
        monkeypatch.setattr(feed_module, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", traced)
    released = {}
    run = tmp_path / "run"
    run.mkdir()
    for step, store in _writer_matrix(run, "plain", enable=lambda _s, _d: None):
        released[step] = _rows(store)
    monkeypatch.undo()
    assert statements, "the trace saw the writers"
    feed_statements = [
        text
        for text in statements
        if "chunks_vec_feed" in text and not text.startswith("SELECT 1 FROM main.sqlite_master")
    ]
    assert feed_statements == []
    assert not [text for text in statements if "secure_delete" in text]
    for step_rows in released.values():
        assert not any("feed" in name for _type, name in step_rows["schema"])

    # The same writes with every feed probe answering "not enabled" give the same tables.
    monkeypatch.setattr(feed_module, "vector_feed_enabled", lambda *_args, **_kwargs: False)
    # Chunk ids depend on the watch root's path, so the second run reuses the same paths.
    shutil.rmtree(run)
    run.mkdir()
    patched = {
        step: _rows(store)
        for step, store in _writer_matrix(run, "plain", enable=lambda _s, _d: None)
    }
    assert patched == released


def _replay_step(store: Path, before: dict[str, bytes], before_seq: int) -> None:
    """The feed entries a step appended turn the prior vector set into the new one."""
    after = _vectors(store)
    state = _state(store)
    current: dict[str, bytes | None] = dict(before)
    for _seq, op, chunk_id, payload in _feed(store, before_seq):
        if op == "insert":
            assert chunk_id not in current
            current[chunk_id] = payload
        else:
            assert chunk_id in current
            del current[chunk_id]
    assert current == after
    assert not state.diverged and state.vector_rows == len(after)
    entries = _feed(store)
    assert state.last_seq == (entries[-1][0] if entries else state.floor_seq)
    with closing(_connect(store)) as conn:
        assert state.live_chunks == conn.execute("SELECT count(*) FROM chunks").fetchone()[0]


@pytest.mark.parametrize("opt_in", ["both", "source-only", "destination-only"])
def test_enabled_feed_records_exactly_the_committed_vector_changes(
    tmp_path: Path, opt_in: str
) -> None:
    def enable(source: Path, destination: Path) -> None:
        if opt_in in ("both", "source-only"):
            _enable(source)
        if opt_in in ("both", "destination-only"):
            _enable(destination)

    enabled = {"source": opt_in != "destination-only", "destination": opt_in != "source-only"}
    seen: dict[Path, tuple[dict[str, bytes], int]] = {}
    for step, store in _writer_matrix(tmp_path, opt_in, enable=enable):
        role = "destination" if step.startswith("copy") else "source"
        if not enabled[role]:
            with closing(_connect(store)) as conn:
                assert read_vector_feed_state(conn) is None
            continue
        before, before_seq = seen.get(store, ({}, 0))
        if step == "build":
            # A certified full build starts a new lineage over what it committed.
            state = _state(store)
            assert state.feed_entries == 0 and _feed(store) == []
            assert state.vector_rows == len(_vectors(store)) and not state.diverged
        else:
            _replay_step(store, before, before_seq)
        seen[store] = (_vectors(store), _state(store).last_seq)
        if step == "update":
            assert {op for _seq, op, _id, _payload in _feed(store, before_seq)} == {
                "insert",
                "delete",
            }

    # A copied chunk whose source has no vector: a chunk without a vector, no feed entry.
    root = _watch(tmp_path, "gap-watch")
    source = _configured(tmp_path, "gap-source", root)
    destination = _configured(tmp_path, "gap-destination", root)
    _build(source, root, full=True)
    _empty(destination)
    _enable(destination)
    with closing(_connect(source)) as conn:
        chunk_id = conn.execute("SELECT chunk_id FROM chunks WHERE path = 'b.md'").fetchone()[0]
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))
    copy_paths(source=source, destination=destination, watch_root=root, paths=["a.md", "b.md"])
    _replay_step(destination, {}, 0)
    state = _state(destination)
    assert state.chunks_without_vector == 1 and chunk_id not in {
        row[2] for row in _feed(destination)
    }


def test_feed_clears_removed_payloads_rolls_back_prunes_by_range_and_keeps_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _watch(tmp_path)
    store = _configured(tmp_path, "store", root)
    _build(store, root, full=True)
    first = _enable(store)
    assert first.floor_seq == first.last_seq == 0 and first.feed_entries == 0
    (root / "e.md").write_text("# Echo\n\necho added\n", encoding="utf-8")
    _build(store, root)
    added = {row[2]: row for row in _feed(store) if row[1] == "insert"}
    removed_bytes = [row[3] for row in added.values()]
    assert removed_bytes and all(removed_bytes)
    (root / "e.md").unlink()
    _build(store, root)
    for _seq, op, chunk_id, payload in _feed(store):
        if op == "insert" and chunk_id in added:
            assert payload is None, "a removed vector's bytes stay in the feed"
    # With secure_delete on the feed's writer connections, the freed bytes are gone from
    # the database file too once the write-ahead log is checkpointed.
    with closing(_connect(store)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    on_disk = chunks_db_path(store).read_bytes()
    # A vector spans overflow pages, so look for windows of it, not the whole run.
    windows = [
        payload[offset : offset + 64]
        for payload in removed_bytes
        if payload is not None
        for offset in range(0, len(payload), 2048)
    ]
    assert windows and not any(window in on_disk for window in windows)

    # A writer failure after some feed rows were written leaves no trace.
    before_rows, before_state = _rows(store), _state(store)
    calls = {"count": 0}
    real_insert = record_vector_insert

    def failing(conn: sqlite3.Connection, chunk_id: str, embedding: bytes) -> None:
        calls["count"] += 1
        real_insert(conn, chunk_id, embedding)
        raise sqlite3.OperationalError("stopped mid-commit")

    monkeypatch.setattr(feed_module, "record_vector_insert", failing)
    (root / "f.md").write_text("# Foxtrot\n\nfoxtrot notes\n", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError, match="stopped mid-commit"):
        _build(store, root)
    monkeypatch.undo()
    assert calls["count"] >= 1
    assert _rows(store) == before_rows and _state(store) == before_state
    (root / "f.md").unlink()

    # Pruning deletes only the excess, as one sequence range.
    monkeypatch.setattr(feed_module, "VECTOR_FEED_TAIL_ROWS", 3)
    for index in range(4):
        (root / f"p{index}.md").write_text(f"# P{index}\n\nprune {index}\n", encoding="utf-8")
        _build(store, root)
    state = _state(store)
    seqs = [row[0] for row in _feed(store)]
    assert state.feed_entries == 3 and seqs == list(range(state.floor_seq + 1, state.last_seq + 1))
    monkeypatch.undo()

    # A certified full rebuild starts a new lineage; a failed certification does not.
    epoch = _state(store).epoch

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("certification interrupted")

    monkeypatch.setattr(build_module, "stamp_identity", refuse)
    with pytest.raises(RuntimeError, match="certification interrupted"):
        _build(store, root, full=True)
    monkeypatch.undo()
    assert _state(store).epoch == epoch and not _state(store).diverged
    _build(store, root, full=True)
    rebuilt = _state(store)
    assert rebuilt.epoch != epoch and rebuilt.feed_entries == 0 and not rebuilt.diverged
    assert rebuilt.vector_rows == len(_vectors(store))


def test_a_write_that_bypasses_the_feed_is_detected_and_reenable_clears_it(
    tmp_path: Path,
) -> None:
    root = _watch(tmp_path)
    store = _configured(tmp_path, "store", root)
    _build(store, root, full=True)
    _enable(store)
    columns = None
    with closing(_connect(store)) as conn:
        row = conn.execute("SELECT * FROM chunks ORDER BY chunk_id LIMIT 1").fetchone()
        columns = len(row)
    slots = ",".join("?" for _ in range(columns))

    def same_count_replace(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (row[0],))
        conn.execute(f"INSERT INTO chunks VALUES ({slots})", row)

    def raw_insert(conn: sqlite3.Connection) -> None:
        conn.execute(f"INSERT INTO chunks VALUES ({slots})", ("raw-chunk", *row[1:]))

    def raw_delete(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (row[0],))

    epochs = {_state(store).epoch}
    for bypass in (same_count_replace, raw_insert, raw_delete):
        with closing(_connect(store)) as conn:
            bypass(conn)
            state = read_vector_feed_state(conn)
            assert state is not None and state.diverged
            with pytest.raises(VectorFeedDiverged):
                state.check()
        cleared = _enable(store)
        assert not cleared.diverged and cleared.epoch not in epochs
        epochs.add(cleared.epoch)


def _steps(conn: sqlite3.Connection, action: Callable[[], object]) -> int:
    count = {"steps": 0}

    def tick() -> int:
        count["steps"] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        action()
    finally:
        conn.set_progress_handler(None, 1)
    return count["steps"]


def _synthetic(tmp_path: Path, name: str, chunks: int) -> Path:
    """A store of ``chunks`` synthetic rows with vectors, its feed enabled and as long."""
    root = tmp_path / f"{name}-watch"
    root.mkdir()
    store = _configured(tmp_path, name, root)
    _empty(store)
    dim = 4096
    with closing(_connect(store)) as conn:
        conn.execute("BEGIN")
        for index in range(chunks):
            chunk_id = f"{name}-{index:06d}"
            conn.execute(
                "INSERT INTO chunks (chunk_id, path, watch_root, kind, section_index, "
                "window_index, heading_depth, body, body_hash, wikilinks_json, tags_json, "
                "dataview_fields_json, embeds_json, ingest_time) "
                "VALUES (?, ?, ?, 'markdown', 0, 0, 0, 'body', 'h', '[]', '[]', '{}', '[]', 't')",
                (chunk_id, f"n{index}.md", str(root)),
            )
            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32([float(index % 7)] * dim)),
            )
        conn.execute("COMMIT")
    _enable(store)
    with closing(_connect(store)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            f"INSERT INTO {FEED_TABLE} (op, chunk_id, embedding) VALUES ('delete', ?, NULL)",
            [(f"{name}-{index:06d}",) for index in range(chunks)],
        )
        conn.execute(f"UPDATE {FEED_STATE_TABLE} SET last_seq = ?", (chunks,))
        conn.execute("COMMIT")
    return store


def test_feed_figures_and_fixed_updates_cost_the_same_at_any_store_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    costs: dict[str, list[int]] = {"read": [], "show": [], "update": []}
    real_read = read_vector_feed_state
    for name, chunks in (("small", 30), ("large", 300)):
        store = _synthetic(tmp_path, name, chunks)
        with closing(_connect(store)) as conn:
            costs["read"].append(_steps(conn, partial(read_vector_feed_state, conn)))
            monkeypatch.setattr(feed_module, "VECTOR_FEED_TAIL_ROWS", chunks - 1)

            def fixed_update(conn: sqlite3.Connection = conn, name: str = name) -> None:
                conn.execute("BEGIN IMMEDIATE")
                record_vector_delete(conn, f"{name}-000003")
                record_vector_insert(conn, f"{name}-000003", b"\1" * 16)
                record_feed_chunk_rows(conn, inserted=1, deleted=1)
                prune_vector_feed(conn)
                conn.execute("ROLLBACK")

            costs["update"].append(_steps(conn, fixed_update))
            monkeypatch.undo()

        def measured(conn: sqlite3.Connection) -> VectorFeedState | None:
            result: list[VectorFeedState | None] = []
            costs["show"].append(_steps(conn, lambda: result.append(real_read(conn))))
            return result[0]

        monkeypatch.setattr(feed_module, "read_vector_feed_state", measured)
        arguments = argparse.Namespace(
            store=store, json=True, index_command="vector-feed", vector_feed_command="show"
        )
        assert dispatch_index(arguments, argparse.ArgumentParser()) == 0
        monkeypatch.undo()
        shown = json.loads(capsys.readouterr().out)
        assert shown["enabled"] is True and shown["live_chunks"] == chunks
        assert shown["vector_rows"] == chunks and shown["feed_entries"] == chunks
        assert shown["chunks_without_vector"] == 0 and shown["diverged"] is False
    for label, (small, large) in costs.items():
        assert small == large > 0, (label, small, large)
