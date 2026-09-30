"""Per-file store operations: copy between stores, removal, and proportional keyword deletion."""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

from palace.index._errors import IndexError
from palace.index.build import build
from palace.index.config import chunks_db_path
from palace.index.core import _delete_rows, _insert_fts_chunk
from palace.index.schema import FTS_ROWID_MAP_KEY, init_db, verify_fts_rowid_map
from palace.index.store_ops import copy_paths, remove_paths
from palace.index.update import update
from palace.metadata import (
    DocumentMetadata,
    MetadataFilter,
    metadata_generation,
    replace_documents,
)
from palace.search import search
from palace.watch.config import WatchRootsConfig, default_config_path
from palace.writer_lock import WriterLockTimeout, writer_lock

from .conftest import FakeEmbedder

FILES = {
    "a.md": "# Alpha\n\nalpha orchard lantern notes\n",
    "b.md": "# Beta\n\nbeta harbor lantern notes\n",
    "c.md": "# Gamma\n\ngamma quarry notes\n",
    "log.jsonl": '{"text": "lantern log entry"}\n{"text": "second quarry entry"}\n',
}
# Ingestion times differ between independently built stores; everything else must match.
TIMESTAMPS = {"files": "last_indexed_at", "chunks": "ingest_time", "cursors": "last_indexed_at"}


def _connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(chunks_db_path(store)), isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _configured(store: Path, root: Path) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    return store


def _built(store: Path, root: Path) -> Path:
    _configured(store, root)
    assert build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder()).roots
    return store


def _empty(store: Path, root: Path) -> Path:
    _configured(store, root)
    chunks_db_path(store).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(store)) as conn:
        init_db(conn, store=store)
    return store


def _table(conn: sqlite3.Connection, table: str, order: str, drop: str | None) -> list[Any]:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[1] != drop]
    return conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}").fetchall()


def _rows(store: Path, *, timestamps: bool = True) -> dict[str, list[Any]]:
    """Every logical table a copy or removal touches, and the schema itself."""

    def drop(name: str) -> str | None:
        return None if timestamps else TIMESTAMPS.get(name)

    with closing(_connect(store)) as conn:
        return {
            "files": _table(conn, "files", "path", drop("files")),
            "chunks": _table(conn, "chunks", "chunk_id", drop("chunks")),
            "cursors": _table(conn, "jsonl_cursors", "path", drop("cursors")),
            "vectors": conn.execute(
                "SELECT chunk_id, embedding FROM chunks_vec ORDER BY chunk_id"
            ).fetchall(),
            "keyword": conn.execute(
                "SELECT rowid, chunk_id, body FROM chunks_fts ORDER BY chunk_id"
            ).fetchall(),
            "lookup": conn.execute(
                "SELECT chunk_id, fts_rowid FROM chunks_fts_rows ORDER BY chunk_id"
            ).fetchall()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts_rows'").fetchone()
            else ["absent"],
            "meta": conn.execute("SELECT * FROM index_meta ORDER BY key").fetchall(),
            "documents": conn.execute(
                "SELECT * FROM metadata_documents ORDER BY document_id"
            ).fetchall(),
            "values": conn.execute(
                "SELECT * FROM metadata_values ORDER BY document_id, field, value"
            ).fetchall(),
            "state": conn.execute("SELECT * FROM metadata_state ORDER BY key").fetchall(),
        }


def _hits(store: Path, query: str, **kwargs: Any) -> list[tuple[object, ...]]:
    return [
        (hit.chunk_id, hit.rrf_score, hit.bm25_score, hit.vec_distance)
        for hit in search(query=query, store=store, embedder=FakeEmbedder(), **kwargs)
    ]


def _generation(store: Path) -> str:
    with closing(_connect(store)) as conn:
        return metadata_generation(conn)


def _tag(store: Path, root: Path, document_id: str, path: str, form: str = "order") -> None:
    with closing(_connect(store)) as conn:
        replace_documents(conn, [DocumentMetadata(document_id, {"form": [form]}, str(root), path)])


def _copy_all(source: Path, destination: Path, root: Path, **kwargs: Any) -> Any:
    return copy_paths(source=source, destination=destination, watch_root=root, **kwargs)


def _copy_and_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    for name in ("a.md", "b.md"):
        (root / name).write_text(FILES[name], encoding="utf-8")
    control = _built(tmp_path / "control", root)  # indexed from exactly a.md and b.md
    for name in ("c.md", "log.jsonl"):
        (root / name).write_text(FILES[name], encoding="utf-8")
    source = _built(tmp_path / "source", root)
    assert _rows(source)["cursors"], "the JSONL file must leave a cursor to copy"
    _tag(source, root.resolve(), "doc-a", "a.md")
    queries = ("lantern", "gamma quarry", "notes", "entry")

    # Equal corpus, in either order: rows (timestamps included) and search are identical, and
    # nothing is embedded.
    monkeypatch.setattr(
        "palace.index.embedder_config.resolve_embedder",
        lambda *_a, **_k: pytest.fail("a copy embedded something"),
    )
    names = list(FILES)
    whole = _empty(tmp_path / "whole", root)
    assert _copy_all(source, whole, root, paths=names).copied == 4
    backward = _empty(tmp_path / "backward", root)
    assert _copy_all(source, backward, root, paths=list(reversed(names))).copied == 4
    monkeypatch.undo()
    source_rows = _rows(source)
    for table in ("files", "chunks", "cursors", "vectors", "keyword", "documents", "values"):
        assert _rows(whole)[table] == source_rows[table], table
    for query in queries:
        assert _hits(whole, query) == _hits(source, query)
        assert _hits(backward, query) == _hits(source, query)
    # An unchanged single-file refresh changes nothing observable.
    before = _rows(whole)
    refresh = _copy_all(source, whole, root, paths=["a.md"])
    assert refresh.copied == 1 and not refresh.metadata_changed and _rows(whole) == before
    for query in queries:
        assert _hits(whole, query) == _hits(source, query)

    # Filtered copy: matches a store indexed from exactly those files; metadata travels and
    # filters retrieval.
    filtered = _empty(tmp_path / "filtered", root)
    first = _copy_all(source, filtered, root, paths=["a.md", "b.md"])
    assert first.copied == 2 and first.metadata_changed
    control_rows, filtered_rows = (
        _rows(control, timestamps=False),
        _rows(filtered, timestamps=False),
    )
    for table in ("files", "chunks", "vectors"):
        assert filtered_rows[table] == control_rows[table], table
    assert [row[1:] for row in filtered_rows["keyword"]] == [
        row[1:] for row in control_rows["keyword"]
    ]
    for query in ("lantern", "alpha orchard", "notes"):
        assert _hits(filtered, query) == _hits(control, query)
    assert [row[0] for row in filtered_rows["documents"]] == ["doc-a"]
    generation = _generation(filtered)
    again = _copy_all(source, filtered, root, paths=["a.md", "b.md"])
    assert not again.metadata_changed and _generation(filtered) == generation
    # Changed metadata is replaced once and filters retrieval.
    _tag(source, root.resolve(), "doc-a", "a.md", form="motion")
    changed = _copy_all(source, filtered, root, paths=["a.md"])
    assert changed.metadata_changed and _generation(filtered) != generation
    motion = [MetadataFilter("form", any_of=("motion",))]
    assert [hit[0] for hit in _hits(filtered, "lantern", filters=motion)] == [
        hit[0] for hit in _hits(source, "lantern", filters=motion)
    ]
    # A file the destination holds but the source no longer does leaves the destination,
    # with its metadata, even though the source kept that file's metadata.
    _tag(source, root.resolve(), "doc-c", "c.md")
    assert _copy_all(source, filtered, root, paths=["c.md"]).copied == 1
    (root / "c.md").unlink()
    update(store=source, watch_root=root, paths=[root / "c.md"], embedder=FakeEmbedder())
    gone = _copy_all(source, filtered, root, paths=["c.md"])
    assert (gone.copied, gone.removed) == (0, 1) and gone.metadata_changed
    after = _rows(filtered)
    assert not [row for table in ("files", "chunks") for row in after[table] if "c.md" in row]
    assert [row[0] for row in after["documents"]] == ["doc-a"]
    with closing(_connect(filtered)) as conn:
        verify_fts_rowid_map(conn)

    # An older source whose tied documents were indexed out of name order keeps that order:
    # the copy converts the source and reuses its keyword rowids.
    tie_root = tmp_path / "tie-watch"
    tie_root.mkdir()
    (tie_root / "y.md").write_text("# Same\n\ntied lantern words\n", encoding="utf-8")
    tie_source = _built(tmp_path / "tie-source", tie_root)
    (tie_root / "b.md").write_text("# Same\n\ntied lantern words\n", encoding="utf-8")
    update(
        store=tie_source, watch_root=tie_root, paths=[tie_root / "b.md"], embedder=FakeEmbedder()
    )
    _pre_phase(tie_source)
    tie_copy = _empty(tmp_path / "tie-copy", tie_root)
    assert _copy_all(tie_source, tie_copy, tie_root, paths=["b.md", "y.md"]).copied == 2
    assert _hits(tie_copy, "tied lantern") == _hits(tie_source, "tied lantern")


def _refusals_atomicity_and_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    for name, body in FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    source = _built(tmp_path / "source", root)
    _tag(source, root.resolve(), "doc-a", "a.md")

    def refused(destination: Path, match: str, **kwargs: object) -> None:
        before = _rows(destination)
        arguments: dict[str, Any] = {
            "source": source,
            "destination": destination,
            "watch_root": root,
            "paths": ["a.md"],
        }
        arguments.update(kwargs)
        with pytest.raises((IndexError, WriterLockTimeout), match=match):
            copy_paths(**arguments)
        assert _rows(destination) == before

    collision = _empty(tmp_path / "collision", root)
    _tag(collision, root.resolve(), "doc-a", "z.md")
    refused(collision, "belongs to another file")
    refused(source, "same store")
    refused(collision, "does not index watch root", watch_root=tmp_path)
    other_root = _empty(tmp_path / "other-root", root)
    with closing(_connect(other_root)) as conn:
        conn.execute("INSERT INTO files VALUES ('x.md', '/elsewhere', 'markdown', 'h', '[]', 't')")
    refused(other_root, "also indexes watch root")
    # A destination lacking the lookup is not converted by a copy that is refused.
    mismatched = _empty(tmp_path / "mismatched", root)
    with closing(_connect(mismatched)) as conn:
        conn.execute("UPDATE index_meta SET value = 'another-model' WHERE key = 'embed_model'")
        conn.execute("DROP TABLE chunks_fts_rows")
        conn.execute("DELETE FROM index_meta WHERE key = ?", (FTS_ROWID_MAP_KEY,))
    refused(mismatched, "different embedder identities")
    unstamped_source = tmp_path / "unstamped-source"
    shutil.copytree(source, unstamped_source)
    unstamped = _empty(tmp_path / "unstamped", root)
    for store in (unstamped_source, unstamped):
        with closing(_connect(store)) as conn:
            conn.execute("DELETE FROM index_meta WHERE key LIKE 'embed_%'")
    refused(unstamped, "embedding-identity mismatch", source=unstamped_source)
    malformed = _empty(tmp_path / "malformed", root)
    with closing(_connect(malformed)) as conn:
        conn.execute("DELETE FROM metadata_state WHERE key = 'generation'")
    refused(malformed, "invalid metadata generation")
    with pytest.raises(IndexError, match="invalid metadata generation"):
        remove_paths(store=malformed, watch_root=root, paths=["a.md"])

    # The source's writer lock is honored: a copy waits for its holder, then refuses.
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with writer_lock(source, operation="paused rebuild"):
            held.set()
            release.wait(10)

    target = _empty(tmp_path / "target", root)
    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(10)
    try:
        refused(target, "paused rebuild", lock_timeout=0.2)
    finally:
        release.set()
        thread.join()

    # A failure after the first file's rows are written leaves the destination unchanged.
    calls = {"count": 0}

    def failing(conn: sqlite3.Connection, chunk_id: str, body: str, **kwargs: Any) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise sqlite3.OperationalError("injected failure")
        _insert_fts_chunk(conn, chunk_id, body, **kwargs)

    monkeypatch.setattr("palace.index.store_ops._insert_fts_chunk", failing)
    before = _rows(target)
    with pytest.raises(sqlite3.OperationalError):
        copy_paths(source=source, destination=target, watch_root=root, paths=["a.md", "b.md"])
    assert _rows(target) == before
    monkeypatch.undo()

    # Removal: an interrupted removal leaves everything; a complete one leaves no row, and the
    # generation moves only when metadata changed.
    assert copy_paths(source=source, destination=target, watch_root=root, paths=list(FILES)).copied
    deletes = {"count": 0}

    def interrupted(**kwargs: Any) -> int:
        deletes["count"] += 1
        if deletes["count"] > 1:
            raise sqlite3.OperationalError("stopped mid-removal")
        return _delete_rows(**kwargs)

    monkeypatch.setattr("palace.index.store_ops._delete_rows", interrupted)
    before = _rows(target)
    with pytest.raises(sqlite3.OperationalError):
        remove_paths(store=target, watch_root=root, paths=["a.md", "b.md"])
    assert _rows(target) == before
    monkeypatch.undo()
    generation = _generation(target)
    tagged = remove_paths(store=target, watch_root=root, paths=["a.md"])
    assert tagged.removed == 1 and tagged.metadata_changed and _generation(target) != generation
    generation = _generation(target)
    untagged = remove_paths(store=target, watch_root=root, paths=[str(root / "b.md")])
    assert untagged.removed == 1 and not untagged.metadata_changed
    assert _generation(target) == generation
    remove_paths(store=target, watch_root=root, paths=["c.md", "log.jsonl"])
    after = _rows(target)
    for table in ("files", "chunks", "cursors", "vectors", "keyword", "lookup", "documents"):
        assert not after[table], table
    with closing(_connect(target)) as conn:
        verify_fts_rowid_map(conn)


def test_copy_and_remove_named_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Copy embeds nothing and reproduces rows and search; refusals and failures change nothing;
    removal leaves no row."""
    for part in ("first", "second"):
        (tmp_path / part).mkdir()
    _copy_and_equality(tmp_path / "first", monkeypatch)
    _refusals_atomicity_and_removal(tmp_path / "second", monkeypatch)


@contextmanager
def _traced(monkeypatch: pytest.MonkeyPatch, store: Path) -> Iterator[list[tuple[int, str, int]]]:
    """Record every statement palace issues against this store: its connection, text and the
    connection's VM step count when it started."""
    records: list[tuple[int, str, int]] = []
    real_connect = sqlite3.connect
    target = str(chunks_db_path(store).resolve())

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(database, *args, **kwargs)
        if target in str(database):
            steps = [0]

            def count() -> int:
                steps[0] += 1
                return 0

            def trace(statement: str) -> None:
                records.append((id(conn), statement, steps[0]))

            conn.set_progress_handler(count, 10)
            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        yield records
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)


def _pre_phase(store: Path) -> None:
    """Remove both schema objects this phase adds, as a store written before it lacks them."""
    with closing(_connect(store)) as conn:
        conn.execute("DROP TABLE chunks_fts_rows")
        conn.execute("DROP INDEX idx_chunks_root_path")
        conn.execute("DELETE FROM index_meta WHERE key = ?", (FTS_ROWID_MAP_KEY,))
        assert _phase_objects(conn) == (False, False)


def _phase_objects(conn: sqlite3.Connection) -> tuple[bool, bool]:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    return "chunks_fts_rows" in names, "idx_chunks_root_path" in names


CONVERSION = "INSERT INTO chunks_fts_rows (chunk_id, fts_rowid) SELECT"
# The one-time conversion's statements: its fill, its consistency check and its index.
CONVERSION_WORK = (
    CONVERSION,
    "LEFT JOIN chunks_fts_rows AS r",
    "LEFT JOIN chunks_fts AS f",
    "DELETE FROM chunks_fts_rows\n",
    "CREATE INDEX IF NOT EXISTS idx_chunks_root_path",
)
FULL_SCAN = re.compile(r"\bSCAN (?:main\.|source\.)?(chunks|chunks_fts_rows|files)\b(?! USING)")


def _bulk(store: Path, root: Path) -> None:
    """20,000 unrelated canonical chunks and keyword rows the operations must not read."""
    with closing(_connect(store)) as conn:
        conn.execute("BEGIN")
        conn.execute(
            "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 19999) "
            "INSERT INTO chunks (chunk_id, path, watch_root, kind, section_index, window_index, "
            "heading, heading_depth, body, body_hash, frontmatter_json, wikilinks_json, tags_json, "
            "dataview_fields_json, embeds_json, ingest_time) "
            "SELECT 'bulk-' || i, 'bulk/' || i || '.md', ?, 'markdown', 0, 0, NULL, 0, "
            "'bulk filler lantern text', 'h', NULL, '[]', '[]', '[]', '[]', 't' FROM n",
            (str(root.resolve()),),
        )
        for index in range(20000):
            _insert_fts_chunk(conn, f"bulk-{index}", "bulk filler lantern text")
        conn.execute("COMMIT")


def _plans(store: Path, source: Path, statements: list[str]) -> list[str]:
    """Query-plan lines that scan a whole table, for the statements that read the stores."""
    bad: list[str] = []
    with closing(_connect(store)) as conn:
        conn.execute("ATTACH DATABASE ? AS source", (str(chunks_db_path(source)),))
        for statement in statements:
            text = statement.strip()
            if CONVERSION in text or not re.match(r"(?i)(SELECT|DELETE|INSERT)\b", text):
                continue
            if not re.search(r"\b(chunks|chunks_fts_rows|files)\b", text):
                continue
            try:
                plan = conn.execute("EXPLAIN QUERY PLAN " + text).fetchall()
            except sqlite3.Error:
                continue
            bad.extend(f"{text} -> {row[-1]}" for row in plan if FULL_SCAN.search(str(row[-1])))
    return bad


def test_per_file_operations_find_keyword_rows_through_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for layout in ("created", "converted"):
        root = tmp_path / layout / "watch"
        root.mkdir(parents=True)
        for name in ("a.md", "b.md", "c.md"):
            (root / name).write_text(FILES[name], encoding="utf-8")
        source = _built(tmp_path / layout / "source", root)
        target = _empty(tmp_path / layout / "copy", root)
        for store in (source, target):
            _bulk(store, root)
            if layout == "converted":  # genuinely lacking both, as an older store does
                _pre_phase(store)

        with (
            _traced(monkeypatch, source) as source_trace,
            _traced(monkeypatch, target) as copy_trace,
        ):
            # The first write to the source is a copy, so an older source is converted by it.
            copy_paths(source=source, destination=target, watch_root=root, paths=["c.md"])
            (root / "a.md").write_text(
                "# Alpha\n\nalpha orchard lantern revised\n", encoding="utf-8"
            )
            update(store=source, watch_root=root, paths=[root / "a.md"], embedder=FakeEmbedder())
            (root / "b.md").unlink()
            update(store=source, watch_root=root, paths=[root / "b.md"], embedder=FakeEmbedder())
            copy_paths(source=source, destination=target, watch_root=root, paths=["a.md", "c.md"])
            copy_paths(source=source, destination=target, watch_root=root, paths=["a.md"])
            remove_paths(store=target, watch_root=root, paths=["c.md"])
            update(store=source, watch_root=root, paths=[root / "a.md"], embedder=FakeEmbedder())
        for label, trace, store in (("source", source_trace, source), ("copy", copy_trace, target)):
            statements = [statement for _conn, statement, _steps in trace]
            conversions = sum(CONVERSION in statement for statement in statements)
            assert conversions == (1 if layout == "converted" else 0), (layout, label, conversions)
            assert not [s for s in statements if "FROM chunks_fts WHERE chunk_id" in s]
            assert not _plans(store, source, statements), _plans(store, source, statements)
            # Steady-state work stays bounded by the files touched, not the 20,000 unrelated
            # chunks and keyword rows; the one-time conversion is measured apart.
            # A statement's cost runs to the next top-level statement on its connection; the
            # internal statements SQLite issues on its behalf ("-- ...") are charged to it.
            top = [row for row in trace if not row[1].lstrip().startswith("--")]
            steps = []
            for index, (conn_id, statement, before) in enumerate(top):
                following = next((row for row in top[index + 1 :] if row[0] == conn_id), None)
                one_time = any(part in statement + "\n" for part in CONVERSION_WORK)
                if following is not None and not one_time:
                    steps.append((following[2] - before, statement))
            worst = max(steps, default=(0, ""))
            assert worst[0] < 5000, (layout, label, worst)
            with closing(_connect(store)) as conn:
                verify_fts_rowid_map(conn)
                assert _phase_objects(conn) == (True, True)
                assert conn.execute(
                    "SELECT value FROM index_meta WHERE key = ?", (FTS_ROWID_MAP_KEY,)
                ).fetchone() == ("complete",)

    # An interrupted conversion leaves the store unmarked, and the next writer converts it.
    legacy_root = tmp_path / "legacy-watch"
    legacy_root.mkdir()
    (legacy_root / "c.md").write_text(FILES["c.md"], encoding="utf-8")
    legacy = _built(tmp_path / "legacy", legacy_root)
    _pre_phase(legacy)

    def stopped(conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("stopped mid-conversion")

    monkeypatch.setattr("palace.index.schema.verify_fts_rowid_map", stopped)
    with pytest.raises(sqlite3.OperationalError):
        remove_paths(store=legacy, watch_root=legacy_root, paths=["c.md"])
    monkeypatch.setattr("palace.index.schema.verify_fts_rowid_map", verify_fts_rowid_map)
    with closing(_connect(legacy)) as conn:
        marker = conn.execute("SELECT 1 FROM index_meta WHERE key = ?", (FTS_ROWID_MAP_KEY,))
        assert marker.fetchone() is None
        assert _phase_objects(conn) == (False, False)  # the rollback took the new objects too
    remove_paths(store=legacy, watch_root=legacy_root, paths=["c.md"])
    with closing(_connect(legacy)) as conn:
        verify_fts_rowid_map(conn)
        assert _phase_objects(conn) == (True, True)
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
