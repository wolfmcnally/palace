"""Full server/queue/tail witness for first-failure cursor custody."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from palace.daemons.capture.config import BOISE_TZ
from palace.index import server, writer
from palace.index.build import build
from palace.index.core import index_one
from palace.index.cursor import TailCursor
from palace.index.daemon_state import DaemonState, read_state
from palace.index.tail import EventsTail
from palace.reindex.schema import build_change_event
from palace.writer_lock import writer_lock

from .conftest import FakeEmbedder


def test_daemon_failure_preserves_cursor_and_unblocks_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_park: None,
) -> None:
    """Idle EOF and full queues cannot hide an embedding/commit/checkpoint failure."""
    for reason in ("embedding", "cursor", "lock"):
        for full_queue in (False, True):
            _run_failure_case(tmp_path, monkeypatch, reason, full_queue)


def _run_failure_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, full_queue: bool
) -> None:
    base = tmp_path / f"{reason}-{full_queue}"
    base.mkdir()
    store, root = base / "store", base / "source"
    store.mkdir()
    root.mkdir()
    for name in ("0-good", "a", "b", "c"):
        (root / f"{name}.md").write_text(f"## Old\nold{name}\n")
    build(
        store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder(), log=lambda _: None
    )
    for name in ("0-good", "a", "b", "c"):
        (root / f"{name}.md").write_text(f"## New\nnew{name}\n")
    entered, release = threading.Event(), threading.Event()
    blocked, idle = threading.Event(), threading.Event()
    ready, parked, shutdown = threading.Event(), threading.Event(), threading.Event()
    failures: list[BaseException] = []

    class OneQueue(queue.Queue[Any]):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=1)

        def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
            if self.full():
                blocked.set()
            super().put(item, block=block, timeout=timeout)

    class ObservedTail(EventsTail):
        def _wait(self) -> bool:
            idle.set()
            return super()._wait()

    class Controlled(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if "new0-good" in texts[0]:
                return super().embed(texts)
            entered.set()
            assert release.wait(5), "test did not release the exact first item"
            if reason == "embedding":
                raise OSError("fixture embedding failure")
            return super().embed(texts)

    def log(line: str) -> None:
        if "palace index: tailing " in line:
            ready.set()
        if "palace index: PARKED" in line:
            parked.set()

    def index(**kwargs: Any) -> Any:
        kwargs["lock_timeout"] = 0
        return index_one(**kwargs)

    def fail_cursor(self: TailCursor, selected_store: Path) -> None:
        raise OSError("fixture cursor-save failure")

    def run() -> None:
        try:
            assert server.serve(store=store, embedder=Controlled(), _shutdown_event=shutdown) == 0
        except BaseException as exc:
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(server, "_stderr_log", log)
        patch.setattr(server, "EventsTail", ObservedTail)
        patch.setattr(queue, "Queue", OneQueue)
        if reason == "lock":
            patch.setattr(writer, "index_one", index)
        if reason == "cursor":
            patch.setattr(TailCursor, "save", fail_cursor)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert ready.wait(5), failures
            hold = (
                writer_lock(store, operation="fixture-held-build")
                if reason == "lock"
                else nullcontext()
            )
            with hold:
                stamp = datetime.now(BOISE_TZ).isoformat()
                events = store / "events"
                events.mkdir(exist_ok=True)
                selected = ("a", "b", "c") if full_queue else ("a",)
                successful_prefix = reason == "embedding" and full_queue
                if successful_prefix:
                    selected = ("0-good", *selected)
                records = []
                for name in selected:
                    event = build_change_event(
                        ingest_time=stamp,
                        observed_at=stamp,
                        change_kind="modified",
                        watch_root=str(root),
                        path=str(root / f"{name}.md"),
                        relative_path=f"{name}.md",
                        is_directory=False,
                    )
                    records.append(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")))
                (events / f"{datetime.now(BOISE_TZ).date().isoformat()}.jsonl").write_text(
                    "\n".join(records) + "\n"
                )
                assert entered.wait(5), failures
                if full_queue:
                    assert blocked.wait(5), "producer never reached the full-queue seam"
                else:
                    idle.clear()
                    assert idle.wait(5), "tail never observed EOF with the first item pending"
                release.set()
                assert parked.wait(5), failures
                state = read_state(store)
                assert isinstance(state, DaemonState) and state.park_kind == "work-failure"
                if successful_prefix:
                    cursor = TailCursor.load(store)
                    assert cursor.byte_offset == len((records[0] + "\n").encode())
                    assert cursor.last_event_id == json.loads(records[0])["id"]
                else:
                    assert not (store / "meta/index-cursor.json").exists()
                with sqlite3.connect(store / "index/chunks.sqlite") as conn:
                    bodies = dict(conn.execute("SELECT path,body FROM chunks"))
                assert bodies["b.md"] == "## Old\noldb" and bodies["c.md"] == "## Old\noldc"
                assert bodies["a.md"] == ("## New\nnewa" if reason == "cursor" else "## Old\nolda")
        finally:
            release.set()
            shutdown.set()
            thread.join(10)
        assert not thread.is_alive() and not failures
