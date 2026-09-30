"""Phase 0 smoke tests.

These cover the scaffold itself: the package imports, the CLI prints its
version, and every runtime dependency declared in ``pyproject.toml`` actually
loads on this machine. The dependency-load test is the early warning for wheel
resolution failures on Apple Silicon.
"""

from __future__ import annotations

import importlib
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_palace_package_importable(tmp_path: Path) -> None:
    import palace

    assert palace.__version__, "palace.__version__ must be a non-empty string"
    # A checkout-free package must import synchronous APIs without resolving
    # launchd templates. Fresh process and asserted module origin prevent the
    # editable install or this test process's import cache from masking failure.
    package = tmp_path / "palace"
    shutil.copytree(
        Path(palace.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__")
    )
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(tmp_path)!r}); "
        "import palace, palace.cli, palace.index, palace.search, palace.multistore; "
        f"assert Path(palace.__file__).parent == Path({str(package)!r})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Every declared runtime dependency loads: sqlite-vec is imported as
    # ``sqlite_vec``; otherwise the dist-name and import-name match.
    for module_name in ("mcp", "sqlite_vec", "watchdog", "httpx", "onnxruntime", "tokenizers"):
        importlib.import_module(module_name)


def test_palace_cli_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "palace.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert re.match(r"^palace 0\.1\.0\s*$", result.stdout), result.stdout

    # The complete CLI remains importable on Linux, while macOS operations
    # refuse before creating a store, loading a native extension, or running
    # launchctl. Exercise this refusal even when this suite runs on macOS.
    from palace.daemons import launchd
    from palace.reindex._errors import ReindexError
    from palace.reindex.debounce import PathDebouncer
    from palace.reindex.observer import build_observer
    from palace.reindex.server import serve
    from palace.watch.config import WatchRootsConfig

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    store = tmp_path / "absent-store"
    with pytest.raises(ReindexError, match="requires macOS.*palace index build"):
        serve(store=store)
    with pytest.raises(ReindexError, match="requires macOS"):
        build_observer(
            config=WatchRootsConfig(),
            store_root=store,
            debouncer=PathDebouncer(sink=lambda event: None),
            log=lambda message, reason: None,
        )
    assert not store.exists()

    agents = tmp_path / "agents"
    agents.mkdir()
    plist = agents / "retained.plist"
    plist.write_bytes(b"retained configuration")
    for operation in (
        lambda: launchd.install_agent(
            label="test",
            plist_template_path=tmp_path / "missing-template",
            log_relative_path="logs/test.log",
            home=store,
            launch_agents_dir=agents,
            run_bootstrap=False,
        ),
        lambda: launchd.uninstall_agent(
            label="test",
            plist_filename=plist.name,
            log_relative_path="logs/test.log",
            home=store,
            launch_agents_dir=agents,
            run_bootout=False,
        ),
        lambda: launchd.bootstrap(plist),
        lambda: launchd.bootout("test"),
        lambda: launchd.kickstart("test"),
    ):
        with pytest.raises(launchd.LifecycleError, match="require macOS"):
            operation()
    assert plist.read_bytes() == b"retained configuration"
    assert not store.exists()


def test_palace_search_help_lists_rerank_and_expansion_flags() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "palace.cli", "search", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--rerank" in result.stdout
    assert "--no-rerank" in result.stdout
    assert "default: on" in result.stdout
    assert "--hyde" in result.stdout
    assert "--multi-query" in result.stdout
    for subcommand, argument in (("expand", "chunk_id"), ("transcript", "session_id")):
        result = subprocess.run(
            [sys.executable, "-m", "palace.cli", "search", subcommand, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert argument in result.stdout, result.stdout
    # The remaining CLI help contracts consolidated into this family: the
    # top-level help lists search, the search help lists its retrieval flags,
    # status names its verdict words, and vault scaffold names its root flag.
    for argv, expected in (
        (["--help"], ["search"]),
        (["search", "--help"], ["--mode", "--limit", "--json", "--verbose"]),
        (["status", "--help"], ["GREEN", "RED"]),
        (["vault", "scaffold", "--help"], ["vault-root"]),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "palace.cli", *argv],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        for token in expected:
            assert token in result.stdout, (argv, result.stdout)
