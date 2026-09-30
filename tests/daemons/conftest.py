"""Shared fixtures and helpers for capture-daemon tests.

Both ``test_capture_core.py`` and ``test_capture_subagent.py`` boot a real
``CaptureServer`` on an ephemeral loopback port and POST against it. The
fixtures and helpers below are the deduplicated surface those modules share.
``test_capture_lifecycle.py`` uses :func:`lifecycle_env` to exercise the
LaunchAgent install/uninstall against a hermetic ``tmp_path`` instead of the
host's real per-user LaunchAgents directory and the live ``gui/<uid>`` domain.
"""

from __future__ import annotations

import http.client
import json
import platform
import shutil
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from palace.daemons.capture.config import BOISE_TZ
from palace.daemons.capture.server import CaptureServer

__all__ = ["lifecycle_env", "running_daemon", "store_path"]


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """Hermetic palace store rooted in pytest's tmp_path."""
    root = tmp_path / "Palace"
    root.mkdir()
    return root


@pytest.fixture
def running_daemon(store_path: Path) -> Iterator[tuple[str, int, Path, CaptureServer]]:
    """Boot a real CaptureServer on an ephemeral loopback port and yield it."""
    server = CaptureServer(("127.0.0.1", 0), store_path)
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield host, port, store_path, server
    finally:
        server.shutdown()
        server.shutdown_writer()
        server.server_close()
        thread.join(timeout=5.0)


def post_json(host: str, port: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST ``payload`` as JSON to ``/capture`` and return ``(status, parsed_body)``."""
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request(
            "POST",
            "/capture",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        raw = response.read()
        status = response.status
    finally:
        conn.close()
    parsed: dict[str, Any] = json.loads(raw.decode("utf-8")) if raw else {}
    return status, parsed


def today_dir(store: Path) -> Path:
    """Return the ``store/sessions/<today>`` directory in ``America/Boise``."""
    today = datetime.now(BOISE_TZ).date().isoformat()
    return store / "sessions" / today


@dataclass(frozen=True)
class LifecycleEnv:
    """Bundle of hermetic paths for the LaunchAgent lifecycle tests."""

    home: Path
    repo_root: Path
    launch_agents_dir: Path
    store: Path
    stub_uv: Path


@pytest.fixture
def lifecycle_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LifecycleEnv:
    """Hermetic install/uninstall environment rooted in pytest's ``tmp_path``.

    The fixture mirrors the layout the real ``palace capture install`` writes
    into: a fake ``HOME`` with the per-user LaunchAgents subtree and a
    ``palace-data.noindex/`` subtree, plus an executable ``uv`` stub the install can
    read off PATH. The fixture stays inside ``tmp_path``; nothing it produces
    touches the host's real per-user LaunchAgents directory or the live
    ``gui/<uid>`` domain.
    """
    # These are hermetic plist rendering tests; the refusal proof in test_smoke
    # separately exercises Linux with no platform override inherited here.
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    launch_agents_dir = home / "Library" / "LaunchAgents"
    store = home / "palace-data.noindex"
    store.mkdir()

    src_stub = Path(__file__).resolve().parent.parent / "fixtures" / "capture" / "uv-bin-stub.sh"
    stub_uv = tmp_path / "uv"
    shutil.copy2(src_stub, stub_uv)
    stub_uv.chmod(stub_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return LifecycleEnv(
        home=home,
        repo_root=repo_root,
        launch_agents_dir=launch_agents_dir,
        store=store,
        stub_uv=stub_uv,
    )
