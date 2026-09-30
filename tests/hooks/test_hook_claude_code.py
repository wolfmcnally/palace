"""Phase 1.4 — Claude Code Stop / SubagentStop hook tests.

Hook script tests (``test_hook_*``) invoke the committed shell script at
``bin/palace-hook-claude-code`` via ``subprocess.run`` so the run-time
environment matches a real Claude Code session as closely as possible. The
``hook_daemon`` fixture boots a real ``CaptureServer`` and the
``hook_home`` fixture redirects ``HOME`` / settings / log paths under
``tmp_path``.

Install/uninstall/status tests (``test_hooks_*``) call into
``palace.hooks.claude_code`` directly — no need to spin up a subprocess to
exercise pure-Python file mutation.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from palace.daemons.capture.config import BOISE_TZ
from palace.hooks import claude_code as hooks_module
from palace.hooks.claude_code import (
    BACKUP_SUFFIX,
    HOOK_SCRIPT_NAME,
    install,
    resolve_hook_script_path,
    resolve_repo_root,
    status,
    uninstall,
)
from tests.hooks.conftest import HookDaemon, HookEnv

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
HOOK_SCRIPT: Path = REPO_ROOT / "bin" / HOOK_SCRIPT_NAME
FIXTURES: Path = REPO_ROOT / "tests" / "fixtures" / "capture"
STOP_FIXTURE: Path = FIXTURES / "claude-code-stop-hook-input.json"
SUB_FIXTURE: Path = FIXTURES / "claude-code-subagent-stop-hook-input.json"
ACTIVE_FIXTURE: Path = FIXTURES / "claude-code-stop-hook-input-active.json"


# ---------------------------------------------------------------- helpers
def _today_dir(store: Path) -> Path:
    return store / "sessions" / datetime.now(BOISE_TZ).date().isoformat()


def _run_hook(
    stdin_bytes: bytes,
    *,
    capture_url: str,
    hook_log: Path,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = {
        **os.environ,
        "PALACE_CAPTURE_URL": capture_url,
        "PALACE_HOOK_LOG": str(hook_log),
        "HOME": str(home),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [str(HOOK_SCRIPT)],
        input=stdin_bytes,
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
        timeout=10,
    )


def _wait_for_file(path: Path, min_bytes: int = 1, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size >= min_bytes:
            return
        time.sleep(0.05)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = path.read_bytes().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _seed_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- hook script tests
def test_hook_maps_stop_event_to_event_type_stop(
    hook_home: HookEnv, hook_daemon: HookDaemon
) -> None:
    proc = _run_hook(
        STOP_FIXTURE.read_bytes(),
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    hook_daemon.server.writer_worker.flush(timeout=5.0)

    fixture = json.loads(STOP_FIXTURE.read_text(encoding="utf-8"))
    session_file = _today_dir(hook_daemon.store) / f"{fixture['session_id']}.jsonl"
    _wait_for_file(session_file)
    records = _read_jsonl(session_file)
    assert len(records) == 1
    record = records[0]
    assert record["event_type"] == "stop"
    assert record["session_id"] == fixture["session_id"]
    assert record["harness"] == "claude-code"
    assert record["transcript_path"] == fixture["transcript_path"]
    assert record["cwd"] == fixture["cwd"]


def test_hook_maps_subagent_stop_event_to_event_type_subagent_stop(
    hook_home: HookEnv, hook_daemon: HookDaemon
) -> None:
    proc = _run_hook(
        SUB_FIXTURE.read_bytes(),
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    hook_daemon.server.writer_worker.flush(timeout=5.0)

    fixture = json.loads(SUB_FIXTURE.read_text(encoding="utf-8"))
    session_file = _today_dir(hook_daemon.store) / f"{fixture['session_id']}.jsonl"
    _wait_for_file(session_file)
    records = _read_jsonl(session_file)
    assert len(records) == 1
    record = records[0]
    assert record["event_type"] == "subagent_stop"
    assert record["agent_id"] == fixture["agent_id"]
    assert record["agent_type"] == fixture["agent_type"]
    assert record["effort"] == fixture["effort"]
    # parent_transcript_path mirrors transcript_path per the script.
    assert record["parent_transcript_path"] == fixture["transcript_path"]


def test_hook_respects_stop_hook_active(hook_home: HookEnv, hook_daemon: HookDaemon) -> None:
    # Pre-existing log line to detect drift.
    sentinel = "sentinel-line-no-touch\n"
    hook_home.hook_log_path.write_text(sentinel, encoding="utf-8")
    before_size = hook_home.hook_log_path.stat().st_size

    proc = _run_hook(
        ACTIVE_FIXTURE.read_bytes(),
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    hook_daemon.server.writer_worker.flush(timeout=2.0)

    today = _today_dir(hook_daemon.store)
    if today.exists():
        assert not list(today.iterdir()), "stop_hook_active=true must not write any file"

    # No new log line appended.
    assert hook_home.hook_log_path.stat().st_size == before_size
    assert hook_home.hook_log_path.read_text(encoding="utf-8") == sentinel


def test_hook_exits_zero_when_daemon_unreachable(hook_home: HookEnv) -> None:
    # Port 1 is reserved; loopback dial fails fast with ECONNREFUSED.
    proc = _run_hook(
        STOP_FIXTURE.read_bytes(),
        capture_url="http://127.0.0.1:1/capture",
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert hook_home.hook_log_path.exists()
    lines = hook_home.hook_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "POST" in lines[0]
    assert "failed" in lines[0]


def test_hook_exits_zero_on_malformed_stdin(hook_home: HookEnv, hook_daemon: HookDaemon) -> None:
    proc = _run_hook(
        b"not-json garbage\n",
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    hook_daemon.server.writer_worker.flush(timeout=2.0)

    today = _today_dir(hook_daemon.store)
    if today.exists():
        assert not list(today.iterdir()), "malformed stdin must not produce a JSONL line"

    lines = hook_home.hook_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "malformed stdin" in lines[0]


# ---------------------------------------------------------------- install tests
def test_hooks_install_registers_command_in_settings_json(hook_home: HookEnv) -> None:
    rc = install()
    assert rc == 0
    assert hook_home.settings_path.exists()

    text = hook_home.settings_path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "settings file must end with a single trailing LF"

    data = json.loads(text)
    expected_command = str(resolve_hook_script_path())
    for event in ("Stop", "SubagentStop"):
        entries = data["hooks"][event]
        assert isinstance(entries, list)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matcher"] == "*"
        assert len(entry["hooks"]) == 1
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        assert inner["command"] == expected_command
        assert inner["command"].endswith(HOOK_SCRIPT_NAME)


def test_hooks_install_preserves_existing_non_palace_hooks(hook_home: HookEnv) -> None:
    foreign_pre = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/local/bin/foreign-pretool"}],
    }
    foreign_stop = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "/usr/local/bin/foreign-stop"}],
    }
    _seed_settings(
        hook_home.settings_path,
        {"hooks": {"PreToolUse": [foreign_pre], "Stop": [foreign_stop]}},
    )

    rc = install()
    assert rc == 0
    data = json.loads(hook_home.settings_path.read_text(encoding="utf-8"))

    # Foreign entries survive byte-for-byte (structural equality is sufficient
    # because json round-trip preserves dict order in py3.7+).
    assert data["hooks"]["PreToolUse"] == [foreign_pre]
    stops = data["hooks"]["Stop"]
    assert foreign_stop in stops
    palace_stops = [e for e in stops if e != foreign_stop]
    assert len(palace_stops) == 1
    assert palace_stops[0]["hooks"][0]["command"].endswith(HOOK_SCRIPT_NAME)


def test_hooks_install_replaces_prior_palace_entry(hook_home: HookEnv) -> None:
    stale_command = "/some/old/checkout/bin/palace-hook-claude-code"
    stale_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": stale_command}],
    }
    _seed_settings(
        hook_home.settings_path,
        {"hooks": {"Stop": [stale_entry], "SubagentStop": [stale_entry]}},
    )

    rc = install()
    assert rc == 0
    data = json.loads(hook_home.settings_path.read_text(encoding="utf-8"))
    expected = str(resolve_hook_script_path())
    for event in ("Stop", "SubagentStop"):
        entries = data["hooks"][event]
        assert len(entries) == 1
        cmd = entries[0]["hooks"][0]["command"]
        assert cmd == expected
        assert cmd != stale_command


def test_hooks_install_writes_bak(hook_home: HookEnv) -> None:
    payload = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]}}
    _seed_settings(hook_home.settings_path, payload)
    pre_install_bytes = hook_home.settings_path.read_bytes()

    rc = install()
    assert rc == 0
    assert hook_home.backup_path.exists()
    assert hook_home.backup_path.read_bytes() == pre_install_bytes


# ---------------------------------------------------------------- uninstall tests
def test_hooks_uninstall_is_idempotent(
    hook_home: HookEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install() == 0
    capsys.readouterr()  # discard install output

    assert uninstall() == 0
    capsys.readouterr()  # discard first uninstall output

    assert uninstall() == 0
    out = capsys.readouterr().out
    assert "no palace hooks registered" in out


def test_hooks_uninstall_preserves_foreign_entries(hook_home: HookEnv) -> None:
    foreign_stop = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/local/bin/foreign-stop"}],
    }
    _seed_settings(hook_home.settings_path, {"hooks": {"Stop": [foreign_stop]}})

    assert install() == 0
    assert uninstall() == 0

    data = json.loads(hook_home.settings_path.read_text(encoding="utf-8"))
    # Foreign Stop entry survives; palace entry is gone; SubagentStop never had
    # foreign content so the key was pruned.
    assert data["hooks"]["Stop"] == [foreign_stop]
    assert "SubagentStop" not in data["hooks"]


# ---------------------------------------------------------------- status tests
def test_hooks_status_handles_missing_settings_file(hook_home: HookEnv) -> None:
    # No settings file, no hook log file.
    assert not hook_home.settings_path.exists()
    if hook_home.hook_log_path.exists():
        hook_home.hook_log_path.unlink()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = status()
    assert rc == 0
    output = buf.getvalue()
    assert "(no hooks registered)" in output
    assert "(no hook log yet)" in output
    # The /health probe will fail against the real loopback (no daemon in this
    # test). That's fine; we just need the section to be present.
    assert "-- /health --" in output


# ---------------------------------------------------------------- integration
def test_hook_payload_satisfies_capture_schema(hook_home: HookEnv, hook_daemon: HookDaemon) -> None:
    # Fire both fixtures through the hook script; assert both records land
    # under the same session file and the second line's id recomputes.
    proc1 = _run_hook(
        STOP_FIXTURE.read_bytes(),
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc1.returncode == 0, proc1.stderr.decode()
    proc2 = _run_hook(
        SUB_FIXTURE.read_bytes(),
        capture_url=hook_daemon.url,
        hook_log=hook_home.hook_log_path,
        home=hook_home.home,
    )
    assert proc2.returncode == 0, proc2.stderr.decode()

    hook_daemon.server.writer_worker.flush(timeout=5.0)

    fixture = json.loads(STOP_FIXTURE.read_text(encoding="utf-8"))
    session_file = _today_dir(hook_daemon.store) / f"{fixture['session_id']}.jsonl"
    _wait_for_file(session_file)
    records = _read_jsonl(session_file)
    assert len(records) == 2

    stop_record, sub_record = records
    # Schema fields.
    for record in records:
        assert record["harness"] == "claude-code"
        assert record["session_id"] == fixture["session_id"]
        assert "id" in record and len(record["id"]) == 64
        assert "ingest_time" in record
    assert stop_record["event_type"] == "stop"
    assert sub_record["event_type"] == "subagent_stop"
    assert sub_record["agent_type"] == "phase-coder"
    assert sub_record["agent_id"] == "subagent-phase-coder-9f2b1d0a"
    assert sub_record["parent_transcript_path"] == sub_record["transcript_path"]

    # Recompute the second line's id from canonical JSON minus id.
    recorded = sub_record["id"]
    minus_id = {k: v for k, v in sub_record.items() if k != "id"}
    expected = hashlib.sha256(
        json.dumps(minus_id, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert recorded == expected


# ---------------------------------------------------------------- module guards
def test_resolve_repo_root_points_at_palace() -> None:
    assert resolve_repo_root() == REPO_ROOT


def test_hook_script_is_executable() -> None:
    path = resolve_hook_script_path()
    assert path.is_file()
    assert os.access(path, os.X_OK)


def test_hooks_module_exports_expected_names() -> None:
    expected = {
        "install",
        "uninstall",
        "status",
        "cli_install",
        "cli_uninstall",
        "cli_status",
        "resolve_repo_root",
        "resolve_hook_script_path",
        "claude_settings_path",
        "hook_log_path",
        "HooksError",
        "HOOK_SCRIPT_NAME",
        "EVENT_NAMES",
        "BACKUP_SUFFIX",
    }
    assert expected.issubset(set(hooks_module.__all__))
    assert BACKUP_SUFFIX == ".palace.bak"


# ---------------------------------------------------------------- python-side run
def test_python_module_entry_point_dispatches_to_status(
    hook_home: HookEnv,
) -> None:
    """``python -m palace.hooks status`` exits 0 and renders the four blocks."""
    env = {
        **os.environ,
        "HOME": str(hook_home.home),
        "PALACE_CLAUDE_SETTINGS_PATH": str(hook_home.settings_path),
        "PALACE_HOOK_LOG": str(hook_home.hook_log_path),
    }
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "palace.hooks", "status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "-- hook script --" in result.stdout
    assert "-- registered hooks --" in result.stdout
    assert "-- hook log tail --" in result.stdout
    assert "-- /health --" in result.stdout
