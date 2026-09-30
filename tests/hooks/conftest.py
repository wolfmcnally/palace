"""Shared fixtures for ``tests/hooks/``.

The fixtures here keep both harnesses' hook tests hermetic:

- ``hook_home`` redirects ``HOME``, ``PALACE_CLAUDE_SETTINGS_PATH``,
  ``CODEX_HOME``, and ``PALACE_HOOK_LOG`` under ``tmp_path`` so neither
  ``~/.claude/settings.json``, ``~/.codex/config.toml`` nor
  ``~/palace-data.noindex/logs/`` is ever touched. The Codex parent directory is
  pre-created so the install path does not silently skip; tests that need
  the "no parent dir" branch delete the directory explicitly.
- ``hook_daemon`` boots a real ``CaptureServer`` on ``127.0.0.1:0`` and
  exports ``PALACE_CAPTURE_URL`` pointing at it so the subprocess hook
  scripts hit the test daemon rather than the operator's local one.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from palace.daemons.capture.server import CaptureServer

__all__ = ["HookEnv", "hook_daemon", "hook_home"]


@dataclass(frozen=True)
class HookEnv:
    """Bundle of hermetic paths for the hook tests."""

    home: Path
    settings_path: Path
    backup_path: Path
    hook_log_path: Path
    codex_home: Path
    codex_config_path: Path
    codex_backup_path: Path


@pytest.fixture
def hook_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookEnv:
    """Redirect HOME, settings, and hook log under a hermetic ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    settings_path = home / ".claude" / "settings.json"
    backup_path = settings_path.with_name(settings_path.name + ".palace.bak")
    hook_log_dir = home / "palace-data.noindex" / "logs"
    hook_log_dir.mkdir(parents=True)
    hook_log_path = hook_log_dir / "palace-capture-hook.log"

    codex_home_path = tmp_path / "codex"
    codex_home_path.mkdir()
    codex_config_path = codex_home_path / "config.toml"
    codex_backup_path = codex_config_path.with_name(codex_config_path.name + ".palace.bak")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PALACE_CLAUDE_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("CODEX_HOME", str(codex_home_path))
    monkeypatch.setenv("PALACE_HOOK_LOG", str(hook_log_path))
    # Tests assert hooks log on failure only (no accept lines).
    monkeypatch.delenv("PALACE_HOOK_LOG_ACCEPTS", raising=False)

    return HookEnv(
        home=home,
        settings_path=settings_path,
        backup_path=backup_path,
        hook_log_path=hook_log_path,
        codex_home=codex_home_path,
        codex_config_path=codex_config_path,
        codex_backup_path=codex_backup_path,
    )


@dataclass(frozen=True)
class HookDaemon:
    """Live capture daemon bound to a loopback ephemeral port."""

    url: str
    health_url: str
    host: str
    port: int
    store: Path
    server: CaptureServer


@pytest.fixture
def hook_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HookDaemon]:
    """Boot a real ``CaptureServer`` and export ``PALACE_CAPTURE_URL``."""
    store = tmp_path / "store"
    store.mkdir()
    server = CaptureServer(("127.0.0.1", 0), store)
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()

    capture_url = f"http://{host}:{port}/capture"
    health_url = f"http://{host}:{port}/health"
    monkeypatch.setenv("PALACE_CAPTURE_URL", capture_url)

    try:
        yield HookDaemon(
            url=capture_url,
            health_url=health_url,
            host=host,
            port=port,
            store=store,
            server=server,
        )
    finally:
        server.shutdown()
        server.shutdown_writer()
        server.server_close()
        thread.join(timeout=5.0)
