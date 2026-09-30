"""Tests for the ``palace watch`` CLI subcommand group."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from palace.cli import main as palace_main


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = palace_main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_watch_add_round_trip(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()

    code, out, err = _run(["watch", "add", str(watch_dir), "--store", str(tmp_store)], capsys)
    assert code == 0, err
    assert out.strip() == f"watch: added {watch_dir.resolve()}"

    code, out, _err = _run(["watch", "list", "--store", str(tmp_store)], capsys)
    assert code == 0
    assert out.strip() == str(watch_dir.resolve())

    code, out, _err = _run(["watch", "remove", str(watch_dir), "--store", str(tmp_store)], capsys)
    assert code == 0
    assert out.strip() == f"watch: removed {watch_dir.resolve()}"

    code, out, _err = _run(["watch", "list", "--store", str(tmp_store)], capsys)
    assert code == 0
    assert out.strip() == "(no watch roots configured)"


def test_watch_add_nonexistent_exits_nonzero(
    tmp_store: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = Path("/no/such/path")
    code, out, err = _run(["watch", "add", str(missing), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "does not exist" in lines[0]


def test_watch_add_palace_store_subpath_exits_nonzero(
    tmp_store: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_store / "events"
    events.mkdir()
    code, out, err = _run(["watch", "add", str(events), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "machine-state root" in lines[0]


def test_watch_add_overlapping_exits_nonzero(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)

    code, _out, _err = _run(["watch", "add", str(parent), "--store", str(tmp_store)], capsys)
    assert code == 0

    code, out, err = _run(["watch", "add", str(child), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "overlaps existing watch root" in lines[0]
    assert str(parent.resolve()) in lines[0]


def test_watch_remove_unknown_exits_nonzero(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    not_added = tmp_path / "never"
    not_added.mkdir()
    code, out, err = _run(["watch", "remove", str(not_added), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "no such watch root" in lines[0]


def test_watch_list_empty(
    tmp_store: Path, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(["watch", "list", "--store", str(tmp_store)], capsys)
    assert code == 0
    assert out.strip() == "(no watch roots configured)"


def test_watch_check_indexed(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    note = watch / "notes.md"
    note.write_text("hello")

    _run(["watch", "add", str(watch), "--store", str(tmp_store)], capsys)

    code, out, _err = _run(["watch", "check", str(note), "--store", str(tmp_store)], capsys)
    assert code == 0
    line = out.strip()
    assert line.startswith("indexed: ")
    assert str(note.resolve()) in line
    assert f"(root: {watch.resolve()})" in line


def test_watch_check_dotfile_ignored(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    ds = watch / ".DS_Store"
    ds.write_text("")

    _run(["watch", "add", str(watch), "--store", str(tmp_store)], capsys)

    code, out, _err = _run(["watch", "check", str(ds), "--store", str(tmp_store)], capsys)
    assert code == 0
    line = out.strip()
    assert line.startswith("ignored: ")
    assert "reason: dotfile" in line


def test_watch_check_gitignore_ignored(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / ".gitignore").write_text("*.log\n")
    log = watch / "build.log"
    log.write_text("ignored")

    _run(["watch", "add", str(watch), "--store", str(tmp_store)], capsys)

    code, out, _err = _run(["watch", "check", str(log), "--store", str(tmp_store)], capsys)
    assert code == 0
    line = out.strip()
    assert line.startswith("ignored: ")
    assert "reason: gitignore" in line


def test_watch_check_not_watched(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "elsewhere" / "file.txt"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("not under any watch root")
    code, out, _err = _run(["watch", "check", str(elsewhere), "--store", str(tmp_store)], capsys)
    assert code == 0
    line = out.strip()
    assert line.startswith("not-watched: ")
    assert str(elsewhere.resolve()) in line


def test_watch_add_activate_restarts_then_bootstraps(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--activate restarts the daemon, then bootstrap-walks the new root."""
    from palace.reindex.lifecycle import RESTART_RESTARTED

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()

    calls: list[str] = []

    def _fake_restart() -> str:
        calls.append("restart")
        return RESTART_RESTARTED

    def _fake_bootstrap(*, store: Path, watch_root_filter: Path, **_kwargs: object) -> int:
        calls.append("bootstrap")
        assert store == tmp_store
        assert watch_root_filter == watch_dir.resolve()
        return 0

    bootstrap_mod = importlib.import_module("palace.reindex.bootstrap")
    lifecycle_mod = importlib.import_module("palace.reindex.lifecycle")

    # Treat tmp_store as the canonical store so the restart branch runs
    # without targeting the operator's real ai.palace.reindex daemon.
    monkeypatch.setattr("palace.watch.cli.DEFAULT_STORE", tmp_store)
    monkeypatch.setattr(lifecycle_mod, "restart", _fake_restart)
    monkeypatch.setattr(bootstrap_mod, "bootstrap", _fake_bootstrap)

    code, out, err = _run(
        ["watch", "add", str(watch_dir), "--store", str(tmp_store), "--activate"], capsys
    )
    assert code == 0, err
    # Restart strictly precedes bootstrap so live edits during the walk are caught.
    assert calls == ["restart", "bootstrap"]
    assert f"watch: added {watch_dir.resolve()}" in out
    assert "watch: reindex daemon restarted" in out
    assert f"watch: activated {watch_dir.resolve()}" in out

    # The root really landed in the config.
    code, out, _err = _run(["watch", "list", "--store", str(tmp_store)], capsys)
    assert out.strip() == str(watch_dir.resolve())


def test_watch_add_activate_warns_when_daemon_not_loaded(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A not-installed daemon is a warning, not a failure: seeding still runs."""
    from palace.reindex.lifecycle import RESTART_NOT_LOADED

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()

    bootstrapped: list[Path] = []

    def _fake_bootstrap(*, store: Path, watch_root_filter: Path, **_kw: object) -> int:
        bootstrapped.append(watch_root_filter)
        return 0

    bootstrap_mod = importlib.import_module("palace.reindex.bootstrap")
    lifecycle_mod = importlib.import_module("palace.reindex.lifecycle")

    # Treat tmp_store as the canonical store so the restart branch runs.
    monkeypatch.setattr("palace.watch.cli.DEFAULT_STORE", tmp_store)
    monkeypatch.setattr(lifecycle_mod, "restart", lambda: RESTART_NOT_LOADED)
    monkeypatch.setattr(bootstrap_mod, "bootstrap", _fake_bootstrap)

    code, out, err = _run(
        ["watch", "add", str(watch_dir), "--store", str(tmp_store), "--activate"], capsys
    )
    assert code == 0, err
    # Bootstrap still ran so existing files are seeded.
    assert bootstrapped == [watch_dir.resolve()]
    assert "reindex daemon not loaded" in err
    assert "palace reindex install" in err
    assert f"watch: activated {watch_dir.resolve()}" in out


def test_watch_add_activate_real_bootstrap_seeds_events(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: --activate's real bootstrap walk emits a created event.

    Only the launchctl restart is stubbed; the bootstrap walk runs for real
    (it makes zero network calls) against a one-file temp root and must write
    a synthetic ``created`` event into ``<store>/events/<date>.jsonl``.
    """
    from palace.reindex.lifecycle import RESTART_RESTARTED

    lifecycle_mod = importlib.import_module("palace.reindex.lifecycle")

    monkeypatch.setattr(lifecycle_mod, "restart", lambda: RESTART_RESTARTED)

    watch_dir = tmp_path / "vault"
    watch_dir.mkdir()
    (watch_dir / "note.md").write_text("# hello\n", encoding="utf-8")

    code, out, err = _run(
        ["watch", "add", str(watch_dir), "--store", str(tmp_store), "--activate"], capsys
    )
    assert code == 0, err
    assert f"watch: activated {watch_dir.resolve()}" in out

    events = list((tmp_store / "events").glob("*.jsonl"))
    assert len(events) == 1, "bootstrap must append to a single day's events log"
    body = events[0].read_text(encoding="utf-8")
    assert "note.md" in body
    assert '"created"' in body


def test_watch_add_activate_custom_store_warns_about_canonical_daemon(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-default --store warns and SKIPS the restart (no side effect on
    the canonical daemon), but still seeds the files."""
    restart_calls: list[str] = []
    bootstrapped: list[Path] = []

    def _restart_should_not_run() -> str:
        restart_calls.append("restart")
        return "restarted"

    def _fake_bootstrap(*, store: Path, watch_root_filter: Path, **_kw: object) -> int:
        bootstrapped.append(watch_root_filter)
        return 0

    bootstrap_mod = importlib.import_module("palace.reindex.bootstrap")
    lifecycle_mod = importlib.import_module("palace.reindex.lifecycle")

    monkeypatch.setattr(lifecycle_mod, "restart", _restart_should_not_run)
    monkeypatch.setattr(bootstrap_mod, "bootstrap", _fake_bootstrap)

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()

    code, _out, err = _run(
        ["watch", "add", str(watch_dir), "--store", str(tmp_store), "--activate"], capsys
    )
    assert code == 0, err
    # tmp_store is never the canonical ~/palace-data.noindex, so the warning
    # fires, the canonical daemon is NOT touched, and seeding still runs.
    assert "is not the canonical store" in err
    assert restart_calls == []
    assert bootstrapped == [watch_dir.resolve()]


def test_no_tracebacks_on_any_error(
    tmp_store: Path,
    tmp_path: Path,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every CLI error path emits ONE stderr line, NO stdout, no traceback."""
    # add error
    code, out, err = _run(["watch", "add", "/no/such/path", "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err

    # remove error
    not_added = tmp_path / "never"
    not_added.mkdir()
    code, out, err = _run(["watch", "remove", str(not_added), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err

    # Malformed TOML in the store: list and check should each emit one
    # error line, no traceback.
    config_path = tmp_store / "meta" / "watch-roots.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("this is :: not :: toml\n")

    code, out, err = _run(["watch", "list", "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err

    target = tmp_path / "anywhere"
    target.mkdir()
    code, out, err = _run(["watch", "check", str(target), "--store", str(tmp_store)], capsys)
    assert code == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
