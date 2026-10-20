"""Observable service, durable receipts and supported-job refusal boundaries."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from palace.index._errors import IndexError as BuildError
from palace.index.build import build
from palace.index.config import EMBED_DIM
from palace.index.egress import current_usage
from palace.index.embedder_config import EmbedderConfig, save_embedder_config
from palace.index.scheduler import BuildLimits, BuildProgress
from palace.index.staging import Staging
from palace.search import search
from palace.watch.config import WatchRootsConfig, default_config_path

from .conftest import FakeEmbedder


def _limits(**values: int) -> BuildLimits:
    return replace(
        BuildLimits(
            source_bytes=16384,
            chunks=128,
            plan_bytes=131072,
            max_files=16,
            staging_bytes=1572864,
            turn_inputs=1,
            turn_bytes=4096,
            remote_turn_inputs=1,
        ),
        **values,
    )


def _case(tmp_path: Path, name: str) -> tuple[Path, Path]:
    base = tmp_path / name
    base.mkdir()
    store, root = base / "store", base / "source"
    store.mkdir()
    root.mkdir()
    return store, root


def _bodies(store: Path, name: str) -> list[str]:
    with sqlite3.connect(store / "index/chunks.sqlite") as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT body FROM chunks WHERE path=? ORDER BY section_index,window_index", (name,)
            )
        ]


def _document(count: int, prefix: str = "body") -> str:
    return "".join(f"## S{i}\n{prefix} {i}\n" for i in range(count))


def test_build_rotates_and_publishes_atomically(tmp_path: Path) -> None:
    """A small file beyond a full giant window is queryable before either giant finishes."""
    for concurrency in (1, 3):
        _fair_case(tmp_path, concurrency)


class _Crash(BaseException):
    pass


def test_build_receipts_recover_and_refuse_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual committed rows and provider calls distinguish every recovery seam."""
    for seam in ("before-request", "response", "receipt", "commit", "published"):
        _recovery_case(tmp_path, monkeypatch, seam)

    # Abrupt process death bypasses Python cleanup and releases the actual writer lock.
    store, root = _case(tmp_path, "process-death")
    (root / "note.md").write_text(_document(3))
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, signal, sys; from pathlib import Path; "
            "from palace.index.build import build; "
            "from tests.index.conftest import FakeEmbedder; "
            "from tests.index.test_scheduler import _limits; "
            "build(store=Path(sys.argv[1]), watch_root_filter=Path(sys.argv[2]), "
            "full=True, embedder=FakeEmbedder(), limits=_limits(), log=lambda _: None, "
            "progress=lambda item: os.kill(os.getpid(), signal.SIGKILL) "
            "if item.event == 'receipt' else None)",
            str(store),
            str(root),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()
    assert _bodies(store, "note.md") == []
    resumed = FakeEmbedder()
    build(
        store=store,
        watch_root_filter=root,
        full=True,
        embedder=resumed,
        limits=_limits(),
        log=lambda _: None,
    )
    assert resumed.call_count == 2 and len(_bodies(store, "note.md")) == 3

    # One provider failure must not discard another completed response in its fold group.
    store, root = _case(tmp_path, "sibling-response")
    (root / "a.md").write_text("## A\nreject-a\n")
    (root / "b.md").write_text("## B\nkeep-b\n")

    class RejectA(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if "reject-a" in texts[0]:
                raise RuntimeError("fixture rejected A")
            return super().embed(texts)

    with pytest.raises(BuildError, match="embedding failed for a.md"):
        build(
            store=store,
            watch_root_filter=root,
            full=True,
            embedder=RejectA(max_concurrency=2),
            embed_concurrency=2,
            limits=_limits(),
            log=lambda _: None,
        )
    assert _bodies(store, "b.md") == []
    resumed = FakeEmbedder(max_concurrency=2)
    build(
        store=store,
        watch_root_filter=root,
        full=True,
        embedder=resumed,
        embed_concurrency=2,
        limits=_limits(),
        log=lambda _: None,
    )
    assert resumed.call_count == 1 and _bodies(store, "b.md") == ["## B\nkeep-b"]

    # A completed receipt cannot vouch for modified vector bytes.
    for mutation in ("corrupt", "truncated", "source", "identity"):
        store, root = _case(tmp_path, mutation)
        (root / "note.md").write_text(_document(3))
        fired = False

        def interrupt(item: BuildProgress) -> None:
            nonlocal fired
            if item.event == "receipt" and not fired:
                fired = True
                raise _Crash()

        with pytest.raises(_Crash):
            build(
                store=store,
                watch_root_filter=root,
                full=True,
                embedder=FakeEmbedder(),
                limits=_limits(),
                progress=interrupt,
                log=lambda _: None,
            )
        payload = next((store / "index/build-staging").glob("*/turn-*.f32"))
        if mutation in {"corrupt", "truncated"}:
            data = payload.read_bytes()
            payload.write_bytes(
                data[:1] if mutation == "truncated" else bytes([data[0] ^ 1]) + data[1:]
            )
            fake = FakeEmbedder()
            with pytest.raises(BuildError, match="receipt checksum"):
                build(
                    store=store,
                    watch_root_filter=root,
                    full=True,
                    embedder=fake,
                    limits=_limits(),
                    log=lambda _: None,
                )
            assert fake.call_count == 0
        else:
            if mutation == "source":
                (root / "note.md").write_text(_document(3, "changed"))
            else:
                save_embedder_config(
                    store,
                    EmbedderConfig(
                        provider="openrouter",
                        model="Other/Model",
                        upstream="Elsewhere",
                        dim=EMBED_DIM,
                        published_corpus=True,
                        published_corpus_note="Synthetic fixture corpus",
                        published_corpus_asserted_at="2026-10-20T00:00:00+00:00",
                    ),
                )
            fake = FakeEmbedder()
            logs: list[str] = []
            build(
                store=store,
                watch_root_filter=root,
                full=True,
                embedder=fake,
                limits=_limits(),
                log=logs.append,
            )
            assert fake.call_count == 3 and any("event=reclaimed" in line for line in logs)


def test_build_limits_progress_and_append_ranges(tmp_path: Path) -> None:
    """Hard limits fail at the named boundary; appends outside a frozen JSONL range remain valid."""
    store, root = _case(tmp_path, "limits")
    source = root / "note.md"
    source.write_text("## A\n" + "x" * 58 + "\n")  # exactly 64 UTF-8 bytes
    limits = _limits(source_bytes=64)
    build(
        store=store,
        watch_root_filter=root,
        embedder=FakeEmbedder(),
        limits=limits,
        log=lambda _: None,
    )
    source.write_text(source.read_text() + "x")
    fake = FakeEmbedder()
    with pytest.raises(BuildError, match="source admission limit"):
        build(store=store, watch_root_filter=root, embedder=fake, limits=limits, log=lambda _: None)
    assert fake.call_count == 0 and len(_bodies(store, "note.md")) == 1
    assert source.stat().st_size == 65

    for event, action, message in (
        ("discovered", "vanish", "source vanished before admission"),
        ("admitted-manifest", "edit", "source changed before publication"),
        ("receipt", "edit-active", "source changed before publication"),
    ):
        selected_store, selected_root = _case(tmp_path, action)
        selected_source = selected_root / "note.md"
        selected_source.write_text("## A\nold\n")

        def change(
            item: BuildProgress,
            expected_event: str = event,
            selected_action: str = action,
            changed_source: Path = selected_source,
        ) -> None:
            if item.event == expected_event:
                if selected_action == "vanish":
                    changed_source.unlink()
                else:
                    changed_source.write_text("## A\nnew\n")

        fake = FakeEmbedder()
        with pytest.raises(BuildError, match=message):
            build(
                store=selected_store,
                watch_root_filter=selected_root,
                embedder=fake,
                limits=_limits(),
                progress=change,
                log=lambda _: None,
            )
        assert fake.call_count == (1 if event == "receipt" else 0)
        assert _bodies(selected_store, "note.md") == []

    selected_store, selected_root = _case(tmp_path, "late-reservation")
    (selected_root / "a-small.md").write_text("## A\nsmall\n")
    (selected_root / "z-giant.md").write_text(_document(100))
    fake = FakeEmbedder()
    with pytest.raises(BuildError, match="document staging reservation"):
        build(
            store=selected_store,
            watch_root_filter=selected_root,
            embedder=fake,
            limits=_limits(),
            log=lambda _: None,
        )
    assert fake.call_count == 0
    source.write_text(_document(3))
    for changes, message in (
        ({"chunks": 2}, "chunk admission"),
        ({"plan_bytes": 128}, "serialized-plan"),
        ({"turn_bytes": 1}, "single embedding"),
        ({"staging_bytes": 128}, "staging_bytes must"),
    ):
        selected_store, selected_root = _case(tmp_path, next(iter(changes)))
        (selected_root / "note.md").write_text(_document(3))
        fake = FakeEmbedder()
        with pytest.raises(BuildError, match=message):
            build(
                store=selected_store,
                watch_root_filter=selected_root,
                full=True,
                embedder=fake,
                limits=_limits(**changes),
                log=lambda _: None,
            )
        assert fake.call_count == 0
    source.write_text(_document(100))
    with pytest.raises(BuildError, match="document staging reservation"):
        build(
            store=store,
            watch_root_filter=root,
            full=True,
            embedder=FakeEmbedder(),
            limits=_limits(),
            log=lambda _: None,
        )
    source.write_text("## A\nbody\n")
    (root / "extra.md").write_text("## B\nbody\n")
    failed_progress: list[BuildProgress] = []
    with pytest.raises(BuildError, match="file admission limit"):
        build(
            store=store,
            watch_root_filter=root,
            embedder=FakeEmbedder(),
            limits=_limits(max_files=1),
            progress=failed_progress.append,
            log=lambda _: None,
        )
    assert failed_progress[-1].event == "refused" and failed_progress[-1].documents is None

    store, root = _case(tmp_path, "append")
    stream = root / "events.jsonl"
    original = b'{"text":"one"}\n{"text":"two"}\n'
    stream.write_bytes(original)
    appended = False

    def append(item: BuildProgress) -> None:
        nonlocal appended
        if item.event == "receipt" and not appended:
            appended = True
            with stream.open("ab") as handle:
                handle.write(b'{"text":"three"}\n')

    build(
        store=store,
        watch_root_filter=root,
        embedder=FakeEmbedder(),
        limits=_limits(),
        progress=append,
        log=lambda _: None,
    )
    assert len(_bodies(store, "events.jsonl")) == 2
    with sqlite3.connect(store / "index/chunks.sqlite") as conn:
        assert conn.execute("SELECT last_byte_offset FROM jsonl_cursors").fetchone()[0] == len(
            original
        )
    fake = FakeEmbedder()
    build(store=store, watch_root_filter=root, embedder=fake, limits=_limits(), log=lambda _: None)
    assert fake.call_count == 1 and len(_bodies(store, "events.jsonl")) == 3


def _fair_case(tmp_path: Path, concurrency: int) -> None:
    store, first = _case(tmp_path, f"fair-{concurrency}")
    second = first.parent / "second"
    second.mkdir()
    for root in (first, second):
        (root / "a-giant.md").write_text("## Old\noldbody\n")
    (second / "z-small.md").write_text("## Old\noldsmall\n")
    config = WatchRootsConfig().with_added(first, store_root=store)
    config = config.with_added(second, store_root=store)
    config.save(default_config_path(store))
    build(store=store, full=True, embedder=FakeEmbedder(), log=lambda _: None)
    (first / "a-giant.md").write_text(_document(30, "first"))
    (second / "a-giant.md").write_text(_document(30, "second"))
    (second / "z-small.md").write_text("## Small\nnewsmall\n")
    held = sqlite3.connect(store / "index/chunks.sqlite", isolation_level=None)
    held.execute("BEGIN")
    old_small = "SELECT COUNT(*) FROM chunks WHERE body='## Old\noldsmall'"
    assert held.execute(old_small).fetchone()[0] == 1
    released = threading.Event()

    class Metered(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if concurrency > 1:
                if "first 0" in texts[0]:
                    assert released.wait(5), "the later small turn did not get a worker"
                else:
                    released.set()
            usage = current_usage()
            assert usage is not None
            usage.record(ok=True, prompt_tokens=len(texts), cost_usd=0.001)
            return super().embed(texts)

    observed: list[BuildProgress] = []

    def progress(item: BuildProgress) -> None:
        observed.append(item)
        if item.event == "committed" and item.path == "z-small.md":
            assert _bodies(store, "a-giant.md") == ["## Old\noldbody", "## Old\noldbody"]
            assert search(query="newsmall", store=store, mode="bm25")
            assert held.execute(old_small).fetchone()[0] == 1

    try:
        report = build(
            store=store,
            embedder=Metered(max_concurrency=concurrency),
            embed_concurrency=concurrency,
            limits=_limits(),
            progress=progress,
            log=lambda _: None,
        )
    finally:
        held.close()
    commits = [p.path for p in observed if p.event == "committed"]
    assert commits[0] == "z-small.md" and len(commits) == 3
    assert [r.embedding.requests for r in report.roots] == [30, 31]
    assert report.embedding.requests == 61
    assert len(_bodies(store, "a-giant.md")) == 60
    assert observed[-1].event == "complete" and observed[-1].committed == 3


def _recovery_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str) -> None:
    store, root = _case(tmp_path, seam)
    source = root / "note.md"
    source.write_text("## Old\noldbody\n")
    build(
        store=store,
        watch_root_filter=root,
        full=True,
        embedder=FakeEmbedder(),
        log=lambda _: None,
    )
    source.write_text(_document(3))
    before = source.read_bytes()
    fake = FakeEmbedder()
    fired = False

    def progress(item: BuildProgress) -> None:
        nonlocal fired
        if seam == "receipt" and item.event == "receipt" and not fired:
            fired = True
            raise _Crash()

    with monkeypatch.context() as patch:
        if seam == "before-request":
            fake.raise_on_call = RuntimeError("before-request")
        if seam == "response":
            patch.setattr(Staging, "save", lambda *a, **k: (_ for _ in ()).throw(_Crash()))
        if seam in {"commit", "published"}:
            original = Staging.publish

            def fail_publish(self: Staging, *args: Any) -> None:
                if seam == "published":
                    original(self, *args)
                raise _Crash()

            patch.setattr(Staging, "publish", fail_publish)
        with pytest.raises((_Crash, BuildError)):
            build(
                store=store,
                watch_root_filter=root,
                full=True,
                embedder=fake,
                limits=_limits(),
                progress=progress,
                log=lambda _: None,
            )
    assert source.read_bytes() == before
    if seam in {"before-request", "response", "receipt"}:
        assert _bodies(store, "note.md") == ["## Old\noldbody"]
    else:
        assert len(_bodies(store, "note.md")) == 3
    resumed = FakeEmbedder()
    events: list[BuildProgress] = []
    build(
        store=store,
        watch_root_filter=root,
        full=True,
        embedder=resumed,
        limits=_limits(),
        progress=events.append,
        log=lambda _: None,
    )
    expected = 2 if seam == "receipt" else 0 if seam in {"commit", "published"} else 3
    assert resumed.call_count == expected, seam
    assert len(_bodies(store, "note.md")) == 3
    assert not list((store / "index/build-staging").iterdir())
