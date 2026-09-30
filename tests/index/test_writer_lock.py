"""Concurrent writers, the per-store writer lock and the per-file update.

Real processes contend for the lock where the phase's acceptance names
processes; deterministic in-process interleavings prove the stale-plan
detection where timing alone could not. Every test uses the deterministic
:class:`FakeEmbedder`; child processes construct their own through the
repository's ``tests`` package and never reach the network.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

import palace.index.core as core
from palace.index._errors import IndexError as PalaceIndexError
from palace.index._errors import StalePlanError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.core import commit_plan, index_one, plan_one
from palace.index.embedder_config import EmbedderConfig, resolve_identity, save_embedder_config
from palace.index.update import update
from palace.metadata import DocumentMetadata, MetadataError, replace_documents
from palace.writer_lock import WriterLockTimeout, writer_lock, writer_lock_path

from .conftest import FakeEmbedder

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PRELUDE = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "from tests.index.conftest import FakeEmbedder\n"
    "def wait_for_go(go):\n"
    "    print('ready', flush=True)\n"
    "    while not Path(go).exists():\n"
    "        time.sleep(0.01)\n"
)
_UPDATE_CHILD = (
    _CHILD_PRELUDE + "from palace.index.update import update\n"
    "store, root, go, timeout, *paths = sys.argv[1:]\n"
    "embedder = FakeEmbedder()\n"
    "wait_for_go(go)\n"
    "result = update(store=Path(store), watch_root=Path(root), paths=[Path(p) for p in paths],"
    " embedder=embedder, lock_timeout=float(timeout), log=lambda line: None)\n"
    "print(json.dumps({'batches': embedder.all_batches, 'indexed': result.indexed,"
    " 'removed': result.removed, 'unchanged': result.unchanged,"
    " 'replanned': result.replanned}))\n"
)
_BUILD_CHILD = (
    _CHILD_PRELUDE + "from palace.index.build import build\n"
    "store, root, go, latency = sys.argv[1:]\n"
    "embedder = FakeEmbedder(latency_seconds=float(latency))\n"
    "wait_for_go(go)\n"
    "report = build(store=Path(store), watch_root_filter=Path(root), embedder=embedder,"
    " lock_timeout=60, log=lambda line: None)\n"
    "print(json.dumps({'roots': len(report.roots), 'batches': embedder.all_batches}))\n"
)
_HOLD_CHILD = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from palace.writer_lock import writer_lock\n"
    "with writer_lock(Path(sys.argv[1]), operation='hold-test'):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


_REMOTE_CONFIG = EmbedderConfig(
    provider="openrouter",
    model="Other/Model",
    upstream="Elsewhere",
    dim=EMBED_DIM,
    published_corpus=True,
    published_corpus_note="synthetic fixture corpus",
    published_corpus_asserted_at="2026-09-26T00:00:00-06:00",
)


def _child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PALACE_STORE"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _spawn(script: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=REPO_ROOT,
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_children(children: Sequence[subprocess.Popen[str]], go: Path) -> list[dict[str, Any]]:
    """Wait for every child to report ready, release them together, collect JSON."""
    for child in children:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    go.write_text("go", encoding="utf-8")
    results: list[dict[str, Any]] = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


def _open(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(chunks_db_path(store)), isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _bodies(store: Path, stored: str) -> list[str]:
    """The last line of every stored section body for ``stored``, in section order."""
    with _open(store) as conn:
        rows = conn.execute(
            "SELECT body FROM chunks WHERE path = ? ORDER BY section_index, window_index",
            (stored,),
        ).fetchall()
    return [str(row[0]).splitlines()[-1] for row in rows]


def _row_count(store: Path) -> tuple[int, int, int]:
    with _open(store) as conn:
        return (
            int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
        )


def _seed(store: Path, root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    assert build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder()).roots


class _GatedEmbedder(FakeEmbedder):
    """A fake embedder whose first call blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(timeout=30)
        return super().embed(texts)


@contextmanager
def _lock_hook(
    monkeypatch: pytest.MonkeyPatch, before_first_acquire: Callable[[], None]
) -> Iterator[None]:
    """Run ``before_first_acquire`` once, just before the core's first lock acquisition."""
    fired = threading.Event()

    @contextmanager
    def hooked(store: Path, *, operation: str, timeout: float) -> Iterator[None]:
        if not fired.is_set():
            fired.set()
            before_first_acquire()
        with writer_lock(store, operation=operation, timeout=timeout):
            yield

    monkeypatch.setattr(core, "writer_lock", hooked)
    yield


def test_many_concurrent_updates_embed_each_file_exactly_once(
    tmp_store: Path, tmp_watch_root: Path, tmp_path: Path
) -> None:
    _seed(tmp_store, tmp_watch_root, {"seed.md": "## Seed\nseed body\n"})
    names = [f"worker-{index}.md" for index in range(8)]
    for name in names:
        (tmp_watch_root / name).write_text(f"## {name}\nbody of {name}\n", encoding="utf-8")
    go = tmp_path / "go"
    children = [
        _spawn(
            _UPDATE_CHILD,
            str(tmp_store),
            str(tmp_watch_root),
            str(go),
            "30",
            str(tmp_watch_root / name),
        )
        for name in names
    ]
    results = _run_children(children, go)
    assert [result["indexed"] for result in results] == [1] * 8
    embedded_texts = [text for result in results for batch in result["batches"] for text in batch]
    assert len(embedded_texts) == 8
    assert len(set(embedded_texts)) == 8
    for name in names:
        assert _bodies(tmp_store, name) == [f"body of {name}"]
    assert _row_count(tmp_store)[2] == 9
    # Every child observed the lock file the holder record lives in.
    assert writer_lock_path(tmp_store).is_file()


def test_concurrent_incremental_builds_embed_changed_files_once(
    tmp_store: Path, tmp_watch_root: Path, tmp_path: Path
) -> None:
    _seed(
        tmp_store,
        tmp_watch_root,
        {f"file-{index}.md": f"## F\nbody {index}\n" for index in range(4)},
    )
    for index in range(3):
        (tmp_watch_root / f"file-{index}.md").write_text(
            f"## F\nchanged {index}\n", encoding="utf-8"
        )
    go = tmp_path / "go"
    children = [
        _spawn(_BUILD_CHILD, str(tmp_store), str(tmp_watch_root), str(go), "0.3") for _ in range(2)
    ]
    results = _run_children(children, go)
    assert [result["roots"] for result in results] == [1, 1]
    counts = sorted(len(result["batches"]) for result in results)
    assert counts == [0, 3]
    for index in range(3):
        assert _bodies(tmp_store, f"file-{index}.md") == [f"changed {index}"]


def test_racing_update_replans_stale_plan_to_disk_state(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_watch_root / "note.md"
    _seed(tmp_store, tmp_watch_root, {"note.md": "## A\nversion one\n"})
    identity = resolve_identity(tmp_store)
    logs: list[str] = []

    # An update of the same file that lands while this one is embedding.
    note.write_text("## A\nversion two\n", encoding="utf-8")
    gated = _GatedEmbedder()
    outcomes: list[core.IndexOutcome] = []

    def slow_update() -> None:
        conn = _open(tmp_store)
        try:
            outcomes.append(
                index_one(
                    conn=conn,
                    embedder=gated,
                    identity=identity,
                    file_kind="markdown",
                    change_kind="created",
                    path=note,
                    watch_root=tmp_watch_root,
                    store=tmp_store,
                    lock_timeout=10,
                    log=logs.append,
                )
            )
        finally:
            conn.close()

    thread = threading.Thread(target=slow_update)
    thread.start()
    assert gated.entered.wait(timeout=10)
    note.write_text("## A\nversion three\n", encoding="utf-8")
    fast = update(
        store=tmp_store,
        watch_root=tmp_watch_root,
        paths=[note],
        embedder=FakeEmbedder(),
        lock_timeout=10,
        log=lambda line: None,
    )
    assert fast.indexed == 1
    gated.release.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert any("reason=stale-plan attempt=1" in line for line in logs)
    assert outcomes[0].noop and outcomes[0].noop_reason == "file-hash-unchanged"
    assert _bodies(tmp_store, "note.md") == ["version three"]

    # A commit that lands between the plan's store read and its source read.
    real_prior = core._prior_file_row
    fired = threading.Event()

    def commit_between_reads(conn: sqlite3.Connection, *, watch_root: Path, stored: str) -> Any:
        prior = real_prior(conn, watch_root=watch_root, stored=stored)
        if not fired.is_set():
            fired.set()
            note.write_text("## A\nversion four\n", encoding="utf-8")
            update(
                store=tmp_store,
                watch_root=tmp_watch_root,
                paths=[note],
                embedder=FakeEmbedder(),
                lock_timeout=10,
                log=lambda line: None,
            )
        return prior

    monkeypatch.setattr(core, "_prior_file_row", commit_between_reads)
    logs.clear()
    with _open(tmp_store) as conn:
        outcome = index_one(
            conn=conn,
            embedder=FakeEmbedder(),
            identity=identity,
            file_kind="markdown",
            change_kind="created",
            path=note,
            watch_root=tmp_watch_root,
            store=tmp_store,
            lock_timeout=10,
            log=logs.append,
        )
    monkeypatch.setattr(core, "_prior_file_row", real_prior)
    assert any("reason=stale-plan" in line for line in logs)
    assert outcome.noop and outcome.noop_reason == "file-hash-unchanged"
    assert _bodies(tmp_store, "note.md") == ["version four"]

    # A plan committed against a store another writer moved is refused by the
    # signature check itself, leaving no transaction open.
    gone = tmp_watch_root / "gone.md"
    _seed(tmp_store, tmp_watch_root, {"gone.md": "## G\ngone body\n"})
    with _open(tmp_store) as conn:
        gone.write_text("## G\nplanned body\n", encoding="utf-8")
        plan = plan_one(
            conn=conn,
            file_kind="markdown",
            change_kind="created",
            path=gone,
            watch_root=tmp_watch_root,
            log=lambda line: None,
        )
        gone.write_text("## G\nback body\n", encoding="utf-8")
        update(
            store=tmp_store,
            watch_root=tmp_watch_root,
            paths=[gone],
            embedder=FakeEmbedder(),
            lock_timeout=10,
            log=lambda line: None,
        )
        vectors = FakeEmbedder().embed(plan.embed_inputs)
        with pytest.raises(StalePlanError, match="changed in the store"):
            commit_plan(
                conn=conn, plan=plan, embeddings=vectors, identity=identity, log=lambda line: None
            )
        assert not conn.in_transaction
    assert _bodies(tmp_store, "gone.md") == ["back body"]

    # A delete whose file reappears before its commit is stale, then re-planned.
    gone.unlink()
    logs.clear()

    def recreate_and_index() -> None:
        gone.write_text("## G\nback again\n", encoding="utf-8")
        update(
            store=tmp_store,
            watch_root=tmp_watch_root,
            paths=[gone],
            embedder=FakeEmbedder(),
            lock_timeout=10,
            log=lambda line: None,
        )

    with _lock_hook(monkeypatch, recreate_and_index), _open(tmp_store) as conn:
        outcome = index_one(
            conn=conn,
            embedder=FakeEmbedder(),
            identity=identity,
            file_kind="markdown",
            change_kind="deleted",
            path=gone,
            watch_root=tmp_watch_root,
            store=tmp_store,
            lock_timeout=10,
            log=logs.append,
        )
    assert any("reason=stale-plan" in line for line in logs)
    assert outcome.noop and outcome.noop_reason == "file-hash-unchanged"
    assert _bodies(tmp_store, "gone.md") == ["back again"]

    # A new file deleted (and removed by another writer) while its first
    # index plan embeds is not inserted: the plan is stale and re-planned as
    # a delete, so no passages survive for a file that is gone.
    fresh = tmp_watch_root / "fresh.md"
    fresh.write_text("## F\nfresh body\n", encoding="utf-8")
    fresh_gate = _GatedEmbedder()
    fresh_outcomes: list[core.IndexOutcome] = []
    logs.clear()

    def insert_fresh() -> None:
        conn = _open(tmp_store)
        try:
            fresh_outcomes.append(
                index_one(
                    conn=conn,
                    embedder=fresh_gate,
                    identity=identity,
                    file_kind="markdown",
                    change_kind="created",
                    path=fresh,
                    watch_root=tmp_watch_root,
                    store=tmp_store,
                    lock_timeout=10,
                    log=logs.append,
                )
            )
        finally:
            conn.close()

    thread = threading.Thread(target=insert_fresh)
    thread.start()
    assert fresh_gate.entered.wait(timeout=10)
    fresh.unlink()
    removed = update(
        store=tmp_store,
        watch_root=tmp_watch_root,
        paths=[fresh],
        embedder=FakeEmbedder(),
        lock_timeout=10,
        log=lambda line: None,
    )
    assert removed.removed == 1
    fresh_gate.release.set()
    thread.join(timeout=30)
    assert any("vanished before its commit" in line for line in logs)
    assert fresh_outcomes[0].kind == "deleted"
    assert _bodies(tmp_store, "fresh.md") == []

    # A file that changes existence while an earlier file in the same call
    # embeds is handled as it is on disk when its turn comes.
    late = tmp_watch_root / "late.md"
    early = tmp_watch_root / "early.md"
    early.write_text("## E\nearly body\n", encoding="utf-8")
    batch_gate = _GatedEmbedder()
    batch_results: list[Any] = []

    def batch_update() -> None:
        batch_results.append(
            update(
                store=tmp_store,
                watch_root=tmp_watch_root,
                paths=[early, late],
                embedder=batch_gate,
                lock_timeout=10,
                log=lambda line: None,
            )
        )

    thread = threading.Thread(target=batch_update)
    thread.start()
    assert batch_gate.entered.wait(timeout=10)
    late.write_text("## L\nlate body\n", encoding="utf-8")
    batch_gate.release.set()
    thread.join(timeout=30)
    assert batch_results[0].indexed == 2 and batch_results[0].removed == 0
    assert _bodies(tmp_store, "late.md") == ["late body"]

    # An update that waits behind a whole-tree build commits afterwards.
    build_embedder = _GatedEmbedder()
    (tmp_watch_root / "later.md").write_text("## L\nlater body\n", encoding="utf-8")
    (tmp_watch_root / "note.md").write_text("## A\nversion five\n", encoding="utf-8")
    build_thread = threading.Thread(
        target=lambda: build(
            store=tmp_store,
            watch_root_filter=tmp_watch_root,
            embedder=build_embedder,
            log=lambda line: None,
        )
    )
    build_thread.start()
    assert build_embedder.entered.wait(timeout=10)
    with (
        pytest.raises(WriterLockTimeout, match="operation=build"),
        writer_lock(tmp_store, operation="probe", timeout=0.2),
    ):
        pass
    released_at = [0.0]

    def release_later() -> None:
        time.sleep(0.5)
        released_at[0] = time.monotonic()
        build_embedder.release.set()

    threading.Thread(target=release_later).start()
    waited = update(
        store=tmp_store,
        watch_root=tmp_watch_root,
        paths=[tmp_watch_root / "later.md"],
        embedder=FakeEmbedder(),
        lock_timeout=10,
        log=lambda line: None,
    )
    build_thread.join(timeout=30)
    assert time.monotonic() >= released_at[0] > 0
    # The update planned while the build held the lock; the build then
    # committed the same file, so the plan was stale and re-planned to a noop.
    assert waited.replanned == 1 and waited.unchanged == 1 and waited.indexed == 0
    assert _bodies(tmp_store, "later.md") == ["later body"]
    assert _bodies(tmp_store, "note.md") == ["version five"]

    # An identity-changing full rebuild that lands while an update embeds under
    # the old identity refuses that update's commit; the rebuild itself
    # succeeds because its commits do not assert the identity they replace.
    other = _GatedEmbedder()
    note.write_text("## A\nversion six\n", encoding="utf-8")
    failures: list[BaseException] = []

    def stale_identity_update() -> None:
        try:
            update(
                store=tmp_store,
                watch_root=tmp_watch_root,
                paths=[note],
                embedder=other,
                lock_timeout=10,
                log=lambda line: None,
            )
        except BaseException as exc:  # noqa: BLE001 — collected for the assertion
            failures.append(exc)

    thread = threading.Thread(target=stale_identity_update)
    thread.start()
    assert other.entered.wait(timeout=10)
    save_embedder_config(tmp_store, _REMOTE_CONFIG)
    assert build(
        store=tmp_store,
        watch_root_filter=tmp_watch_root,
        full=True,
        embedder=FakeEmbedder(),
        log=lambda line: None,
    ).roots
    with _open(tmp_store) as conn:
        assert (
            conn.execute("SELECT value FROM index_meta WHERE key = 'embed_provider'").fetchone()[0]
            == "openrouter:Elsewhere"
        )
    other.release.set()
    thread.join(timeout=30)
    assert len(failures) == 1 and isinstance(failures[0], PalaceIndexError)
    assert "embedding-identity mismatch" in str(failures[0])
    assert _bodies(tmp_store, "note.md") == ["version six"]


def test_lock_released_on_sigkill_and_timeout_names_holder(
    tmp_store: Path, tmp_watch_root: Path
) -> None:
    _seed(tmp_store, tmp_watch_root, {"note.md": "## A\nbody\n"})
    holder = _spawn(_HOLD_CHILD, str(tmp_store))
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    started = time.monotonic()
    with writer_lock(tmp_store, operation="after-kill", timeout=2):
        pass
    assert time.monotonic() - started < 2

    holder = _spawn(_HOLD_CHILD, str(tmp_store))
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    try:
        before = _row_count(tmp_store)
        (tmp_watch_root / "note.md").write_text("## A\nchanged\n", encoding="utf-8")
        with pytest.raises(WriterLockTimeout) as blocked:
            update(
                store=tmp_store,
                watch_root=tmp_watch_root,
                paths=[tmp_watch_root / "note.md"],
                embedder=FakeEmbedder(),
                lock_timeout=0.3,
                log=lambda line: None,
            )
        message = str(blocked.value)
        assert f"pid {holder.pid}" in message and "operation=hold-test" in message
        assert _row_count(tmp_store) == before
        assert _bodies(tmp_store, "note.md") == ["body"]
        with pytest.raises(WriterLockTimeout, match=f"pid {holder.pid}"):
            build(
                store=tmp_store,
                watch_root_filter=tmp_watch_root,
                embedder=FakeEmbedder(),
                lock_timeout=0.3,
                log=lambda line: None,
            )
        with _open(tmp_store) as conn, pytest.raises(MetadataError, match=f"pid {holder.pid}"):
            replace_documents(conn, [DocumentMetadata("doc", {"kind": ["x"]})], lock_timeout=0.3)
    finally:
        holder.kill()
        holder.wait(timeout=10)
    with _open(tmp_store) as conn:
        replace_documents(conn, [DocumentMetadata("doc", {"kind": ["x"]})], lock_timeout=2)


def _egress_records(store: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((store / "events" / "cloud-egress").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _check_usage_is_reported_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 19: each build and update reports the embedding requests it made, as the provider
    reported them, in its result, on its failure and as --json, never mixing invocations."""
    import httpx

    from palace.cli import main
    from palace.index.egress import EmbeddingUsage, usage_scope
    from palace.index.embedder import OpenRouterEmbedder

    store = tmp_path / "usage-store"
    root = tmp_path / "usage-root"
    store.mkdir()
    root.mkdir()
    save_embedder_config(store, _REMOTE_CONFIG)
    lock = threading.Lock()
    served = {"requests": 0, "inputs": 0, "known_cost_inputs": 0}
    script: list[str] = []
    overlap = threading.Barrier(2, timeout=10)
    arrivals: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        with lock:
            served["requests"] += 1
            step = script.pop(0) if script else "ok"
        if step == "overlap":  # two updates meet here, so both are in flight at once
            arrivals.append(threading.current_thread().name)
            overlap.wait()
            step = "ok"
        if step == "429":  # a refusal whose body carries a cost that must never be summed
            return httpx.Response(429, json={"usage": {"prompt_tokens": 9, "cost": 99.0}})
        usage: dict[str, Any] = {"prompt_tokens": 10 * len(inputs)}
        if step != "nocost":
            usage["cost"] = 0.001 * len(inputs)
            with lock:
                served["known_cost_inputs"] += len(inputs)
        with lock:
            served["inputs"] += len(inputs)
        data = [{"index": i, "embedding": [0.01] * EMBED_DIM} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data, "provider": "Elsewhere", "usage": usage})

    def embedder(**extra: Any) -> OpenRouterEmbedder:
        return OpenRouterEmbedder(
            store=store,
            model="Other/Model",
            upstream="Elsewhere",
            api_key="synthetic",
            max_batch=2,
            transport=httpx.MockTransport(handler),
            backoff_base_seconds=0,
            _sleep=lambda _seconds: None,
            **extra,
        )

    def reset() -> None:
        served.update(requests=0, inputs=0, known_cost_inputs=0)

    for name, sections in (("a.md", 3), ("b.md", 1)):
        text = "".join(f"## S{index}\nbody {name} {index}\n" for index in range(sections))
        (root / name).write_text(text, encoding="utf-8")
    # A build with concurrent embedding: the probe counts for the invocation, not for a root.
    report = build(
        store=store,
        watch_root_filter=root,
        full=True,
        embedder=embedder(),
        embed_concurrency=2,
        log=lambda _line: None,
    )
    usage = report.embedding
    assert usage.requests == served["requests"] and usage.failed_requests == 0
    assert usage.prompt_tokens == 10 * served["inputs"]
    assert usage.cost_usd == Decimal(str(0.001)) * served["inputs"]
    assert (
        report.roots[0].embedding.requests == served["requests"] - 1
    )  # every request but the probe
    records = _egress_records(store)
    assert len(records) == usage.requests  # the audit records and the usage agree
    assert sum(record["prompt_tokens"] for record in records) == usage.prompt_tokens
    assert sum(Decimal(str(record["cost_usd"])) for record in records) == usage.cost_usd
    reset()
    empty = tmp_path / "empty-usage-root"
    empty.mkdir()
    probe_only = build(
        store=store, watch_root_filter=empty, embedder=embedder(), log=lambda _l: None
    )
    assert (
        probe_only.embedding.requests == served["requests"] == 1
    )  # nothing walked, the probe spent
    # An update with a refused request: counted as failed, its cost never summed.
    reset()
    (root / "a.md").write_text("## S0\nchanged 0\n## S1\nchanged 1\n## S2\nchanged 2\n")
    script[:] = ["429"]
    result = update(
        store=store,
        watch_root=root,
        paths=[root / "a.md"],
        embedder=embedder(),
        log=lambda _l: None,
    )
    assert (result.embedding.requests, result.embedding.failed_requests) == (served["requests"], 1)
    assert result.embedding.cost_usd == Decimal(str(0.001)) * served["known_cost_inputs"]
    assert result.embedding.prompt_tokens == 10 * served["inputs"]
    audited = _egress_records(store)[
        len(records) + 1 :
    ]  # this update's records, the refusal's included
    assert len(audited) == result.embedding.requests
    assert (
        sum(Decimal(str(record["cost_usd"] or 0)) for record in audited)
        == result.embedding.cost_usd
    )
    # A success that reported no cost makes the invocation's cost unknown.
    reset()
    (root / "b.md").write_text("## S0\nchanged b\n")
    script[:] = ["nocost"]
    result = update(
        store=store,
        watch_root=root,
        paths=[root / "b.md"],
        embedder=embedder(),
        log=lambda _l: None,
    )
    assert result.embedding.cost_usd is None and result.embedding.unknown_cost_requests == 1
    # Two updates in flight at once each report only their own requests (one and two).
    (root / "a.md").write_text("## S0\nagain 0\n## S1\nagain 1\n## S2\nagain 2\n")
    (root / "b.md").write_text("## S0\nagain b\n")
    script[:] = ["overlap", "overlap"]
    results: dict[str, Any] = {}

    def run(name: str) -> None:
        results[name] = update(
            store=store,
            watch_root=root,
            paths=[root / name],
            embedder=embedder(),
            log=lambda _l: None,
        )

    threads = [threading.Thread(target=run, args=(name,), name=name) for name in ("a.md", "b.md")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(arrivals) == ["a.md", "b.md"]  # both were in flight together
    assert results["a.md"].embedding.requests == 2 and results["b.md"].embedding.requests == 1
    # A failing update still reports what it spent.
    (root / "b.md").write_text("## S0\nfails\n")
    script[:] = ["429", "429"]
    with pytest.raises(PalaceIndexError) as failed:
        update(
            store=store,
            watch_root=root,
            paths=[root / "b.md"],
            embedder=embedder(max_attempts=2),
            log=lambda _l: None,
        )
    spent = failed.value.embedding_usage  # type: ignore[attr-defined]
    assert (spent.requests, spent.failed_requests) == (2, 2)
    # --json through the real command line: exactly one object on stdout, success and failure.
    monkeypatch.setattr(
        sys.modules["palace.index.update"],
        "resolve_embedder",
        lambda _store: (embedder(), resolve_identity(store)),
    )
    monkeypatch.setattr(
        sys.modules["palace.index.build"],
        "resolve_embedder",
        lambda _store: (embedder(), resolve_identity(store)),
    )
    (root / "b.md").write_text("## S0\nvia the command line\n")
    capsys.readouterr()
    assert (
        main(
            [
                "index",
                "update",
                "--store",
                str(store),
                "--watch-root",
                str(root),
                "--json",
                str(root / "b.md"),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert (
        printed["ok"] is True and printed["indexed"] == 1 and printed["embedding"]["requests"] == 1
    )
    assert main(["index", "build", "--store", str(store), "--watch-root", str(root), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (
        printed["ok"] is True
        and printed["embedding"]["requests"] >= 1
        and len(printed["roots"]) == 1
    )
    (root / "b.md").write_text("## S0\nrefused on the command line\n")
    script[:] = ["429"] * 10
    monkeypatch.setattr(
        sys.modules["palace.index.update"],
        "resolve_embedder",
        lambda _store: (embedder(max_attempts=2), resolve_identity(store)),
    )
    assert (
        main(
            [
                "index",
                "update",
                "--store",
                str(store),
                "--watch-root",
                str(root),
                "--json",
                str(root / "b.md"),
            ]
        )
        == 1
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["embedding"]["failed_requests"] == 2
    # Review findings: a missing store refused before any request, still one JSON object.
    assert (
        main(
            [
                "index",
                "update",
                "--store",
                str(tmp_path / "absent"),
                "--watch-root",
                str(root),
                "--json",
                str(root / "b.md"),
            ]
        )
        == 1
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["embedding"]["requests"] == 0
    # A build whose probe is refused: the exception and the --json failure carry the spent request.
    script[:] = ["429"] * 4
    with pytest.raises(PalaceIndexError) as failed_build:
        build(
            store=store,
            watch_root_filter=root,
            embedder=embedder(max_attempts=1),
            log=lambda _l: None,
        )
    assert failed_build.value.embedding_usage.failed_requests == 1  # type: ignore[attr-defined]
    monkeypatch.setattr(
        sys.modules["palace.index.build"],
        "resolve_embedder",
        lambda _store: (embedder(max_attempts=1), resolve_identity(store)),
    )
    assert main(["index", "build", "--store", str(store), "--watch-root", str(root), "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["embedding"]["failed_requests"] == 1
    script.clear()
    # No configured roots: nothing walked, the probe still reported.
    bare = tmp_path / "bare-store"
    bare.mkdir()
    save_embedder_config(bare, _REMOTE_CONFIG)
    served["requests"] = 0
    unwalked = build(store=bare, embedder=embedder(), log=lambda _l: None)
    assert unwalked.roots == () and unwalked.embedding.requests == served["requests"] == 1
    # A request made before its audit record fails to write is still counted.
    import palace.index.embedder as embedder_module

    def unwritable(**_kwargs: Any) -> None:
        raise OSError("audit sink full")

    (root / "b.md").write_text("## S0\naudit fails\n")
    with monkeypatch.context() as patch:
        patch.setattr(embedder_module, "append_record", unwritable)
        with pytest.raises(PalaceIndexError) as unaudited:
            update(
                store=store,
                watch_root=root,
                paths=[root / "b.md"],
                embedder=embedder(),
                log=lambda _l: None,
            )
    assert unaudited.value.embedding_usage.requests == 1  # type: ignore[attr-defined]
    # A failure writing the final summary still carries everything spent.
    (root / "b.md").write_text("## S0\nsummary fails\n")

    def refuse_summary(line: str) -> None:
        if line.startswith("palace index update:"):
            raise OSError("stderr closed")

    with pytest.raises(OSError) as unlogged:
        update(
            store=store,
            watch_root=root,
            paths=[root / "b.md"],
            embedder=embedder(),
            log=refuse_summary,
        )
    assert unlogged.value.embedding_usage.requests == 1  # type: ignore[attr-defined]
    with usage_scope(EmbeddingUsage()):
        pass  # the scope is re-entrant and leaves no total behind


def test_update_refuses_and_removes_per_contract(
    tmp_store: Path,
    tmp_watch_root: Path,
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_watch_root / ".gitignore").write_text("private/\n", encoding="utf-8")
    (tmp_watch_root / "private").mkdir()
    (tmp_watch_root / "private" / ".gitignore").write_text("!note.md\n", encoding="utf-8")
    (tmp_watch_root / "private" / "note.md").write_text("## P\nprivate\n", encoding="utf-8")
    (tmp_watch_root / ".hidden").mkdir()
    (tmp_watch_root / ".hidden" / "dot.md").write_text("## H\nhidden\n", encoding="utf-8")
    (tmp_watch_root / "sub").mkdir()
    _seed(tmp_store, tmp_watch_root, {"keep.md": "## K\nkeep\n", "drop.md": "## D\ndrop\n"})
    assert _bodies(tmp_store, "private/note.md") == []
    before = _row_count(tmp_store)
    logs: list[str] = []

    def refused(paths: list[Path], match: str, **kwargs: Any) -> None:
        with pytest.raises(PalaceIndexError, match=match):
            update(
                store=kwargs.pop("store", tmp_store),
                watch_root=kwargs.pop("watch_root", tmp_watch_root),
                paths=paths,
                embedder=FakeEmbedder(),
                log=logs.append,
                **kwargs,
            )
        assert _row_count(tmp_store) == before

    refused([tmp_path / "outside.md"], "not under watch root")
    refused([tmp_watch_root / ".hidden" / "dot.md"], "reason=dotfile")
    refused([tmp_watch_root / "private" / "note.md"], "gitignore on private/")
    refused([tmp_watch_root / "private" / "absent.md"], "gitignore on private/")
    refused([tmp_watch_root / ".gitignore"], "ignore files are never indexed")
    refused([tmp_watch_root / "sub"], "is a directory")
    refused([tmp_watch_root], "is the watch root itself")
    refused([tmp_watch_root / "keep.md", tmp_path / "outside.md"], "outside.md")
    refused([], "at least one path")
    refused([tmp_watch_root / "keep.md"], "not a directory", watch_root=tmp_path / "missing")
    refused([tmp_path / "x.md"], "resolves under the store", watch_root=tmp_store)
    assert logs == []

    empty_store = tmp_path / "empty-store"
    empty_store.mkdir()
    refused([tmp_watch_root / "keep.md"], "no index at", store=empty_store)
    assert not chunks_db_path(empty_store).exists()

    # An initialized but empty, unstamped store is exactly the shape init_db
    # would stamp; the update must refuse it without creating identity rows.
    unstamped = tmp_path / "unstamped"
    unstamped.mkdir()
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    _seed(unstamped, empty_root, {})
    with _open(unstamped) as conn:
        conn.execute("DELETE FROM index_meta")
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    refused([tmp_watch_root / "keep.md"], "palace index build --full", store=unstamped)
    with _open(unstamped) as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_meta").fetchone()[0] == 0
    # The named remedy works on that store, and the update then proceeds.
    assert build(
        store=unstamped, watch_root_filter=tmp_watch_root, full=True, embedder=FakeEmbedder()
    ).roots
    assert (
        update(
            store=unstamped,
            watch_root=tmp_watch_root,
            paths=[tmp_watch_root / "keep.md"],
            embedder=FakeEmbedder(),
            log=logs.append,
        ).unchanged
        == 1
    )

    mismatched = tmp_path / "mismatched"
    mismatched.mkdir()
    _seed(mismatched, tmp_watch_root, {})
    save_embedder_config(mismatched, _REMOTE_CONFIG)
    refused(
        [tmp_watch_root / "keep.md"], "embedding-identity mismatch.*build --full", store=mismatched
    )
    # The named remedy re-stamps the populated mismatched store; the update then proceeds.
    assert build(
        store=mismatched, watch_root_filter=tmp_watch_root, full=True, embedder=FakeEmbedder()
    ).roots
    assert (
        update(
            store=mismatched,
            watch_root=tmp_watch_root,
            paths=[tmp_watch_root / "keep.md"],
            embedder=FakeEmbedder(),
            log=logs.append,
        ).unchanged
        == 1
    )

    # An invalid wait is refused before any work.
    refused([tmp_watch_root / "keep.md"], "finite non-negative", lock_timeout=float("inf"))
    refused([tmp_watch_root / "keep.md"], "finite non-negative", lock_timeout=-1)

    # A root named through a symlink alias indexes and removes the same file.
    alias = tmp_path / "alias-root"
    alias.symlink_to(tmp_watch_root, target_is_directory=True)
    (tmp_watch_root / "via-alias.md").write_text("## V\nvia alias\n", encoding="utf-8")
    assert (
        update(
            store=tmp_store,
            watch_root=alias,
            paths=[alias / "via-alias.md"],
            embedder=FakeEmbedder(),
            log=logs.append,
        ).indexed
        == 1
    )
    (tmp_watch_root / "via-alias.md").unlink()
    assert (
        update(
            store=tmp_store,
            watch_root=alias,
            paths=[alias / "via-alias.md"],
            embedder=FakeEmbedder(),
            log=logs.append,
        ).removed
        == 1
    )
    assert _bodies(tmp_store, "via-alias.md") == []

    # A present but unchanged file makes no embedding request.
    quiet = FakeEmbedder()
    result = update(
        store=tmp_store,
        watch_root=tmp_watch_root,
        paths=[tmp_watch_root / "keep.md"],
        embedder=quiet,
        log=logs.append,
    )
    assert quiet.call_count == 0 and result.unchanged == 1
    assert logs[-1].startswith("palace index update: root=") and "unchanged=1" in logs[-1]

    # A deleted file loses its passages; an absent never-indexed file is a clean removal.
    (tmp_watch_root / "drop.md").unlink()
    result = update(
        store=tmp_store,
        watch_root=tmp_watch_root,
        paths=[tmp_watch_root / "drop.md", tmp_watch_root / "never.md"],
        embedder=quiet,
        log=logs.append,
    )
    assert quiet.call_count == 0 and result.removed == 2
    assert _bodies(tmp_store, "drop.md") == []
    assert _row_count(tmp_store)[2] == before[2] - 1

    # Exit codes: 0 with the summary line, 1 with one error line, 2 for usage.
    command = [sys.executable, "-m", "palace.cli", "index", "update", "--store", str(tmp_store)]
    ok = subprocess.run(
        [*command, "--watch-root", str(tmp_watch_root), str(tmp_watch_root / "keep.md")],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stderr.strip().startswith("palace index update: root=") and "paths=1" in ok.stderr
    bad = subprocess.run(
        [*command, "--watch-root", str(tmp_watch_root), str(tmp_path / "outside.md")],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert bad.stderr.count("error:") == 1 and "not under watch root" in bad.stderr
    usage = subprocess.run(
        [*command, str(tmp_watch_root / "keep.md")],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode == 2 and "--watch-root" in usage.stderr
    for invalid in ("nan", "inf", "-1", "soon"):
        rejected = subprocess.run(
            [
                *command,
                "--watch-root",
                str(tmp_watch_root),
                "--lock-timeout",
                invalid,
                str(tmp_watch_root / "keep.md"),
            ],
            cwd=REPO_ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode == 2 and "--lock-timeout" in rejected.stderr, invalid
    for subcommand in ("update", "build"):
        shown = subprocess.run(
            [sys.executable, "-m", "palace.cli", "index", subcommand, "--help"],
            cwd=REPO_ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert shown.returncode == 0 and "--lock-timeout" in shown.stdout
    _check_usage_is_reported_per_invocation(tmp_path, monkeypatch, capsys)
