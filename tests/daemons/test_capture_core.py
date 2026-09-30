"""Capture-daemon core tests.

Boots a real ``CaptureServer`` on ``127.0.0.1:0`` per test, exercises the
HTTP + writer path against the live process, and tears down. The
``running_daemon`` fixture (from ``conftest.py``) exposes
``(host, port, store_path, server)``; ``server.writer_worker.flush()`` makes
queue-drain deterministic so tests read JSONL files only after writes have
been fsynced.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from palace.daemons.capture.ids import canonical_json_bytes, compute_record_id
from palace.daemons.capture.schema import build_record, to_jsonl_bytes
from palace.daemons.capture.server import CaptureServer

from .conftest import post_json, today_dir

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "capture"


def _read_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return result


# --------------------------------------------------------------------- id tests
def test_canonical_id_is_deterministic() -> None:
    record = {
        "ingest_time": "2026-05-17T14:33:00-06:00",
        "event_type": "stop",
        "harness": "claude-code",
        "session_id": "abc",
        "assistant_message": "hi",
        "tool_calls": [],
        "files_touched": [],
    }
    first = compute_record_id(dict(record))
    second = compute_record_id(dict(record))
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_canonical_id_ignores_key_order() -> None:
    a = {
        "ingest_time": "2026-05-17T14:33:00-06:00",
        "event_type": "stop",
        "harness": "claude-code",
        "session_id": "abc",
        "assistant_message": "hi",
    }
    b = {
        "assistant_message": "hi",
        "session_id": "abc",
        "harness": "claude-code",
        "event_type": "stop",
        "ingest_time": "2026-05-17T14:33:00-06:00",
    }
    assert compute_record_id(a) == compute_record_id(b)


def test_canonical_json_handles_non_ascii_roundtrip() -> None:
    record = build_record(
        {
            "event_type": "stop",
            "harness": "claude-code",
            "session_id": "non-ascii-session",
            "assistant_message": "em — dash and naïve text",
        },
        now_iso="2026-05-17T14:33:00-06:00",
    )
    line = to_jsonl_bytes(record)
    # The line re-parses as JSON.
    reparsed = json.loads(line.decode("utf-8"))
    assert reparsed["assistant_message"] == "em — dash and naïve text"
    assert reparsed["event_type"] == "stop"
    # Recomputed id matches.
    minus_id = {k: v for k, v in reparsed.items() if k != "id"}
    recomputed = hashlib.sha256(canonical_json_bytes(minus_id)).hexdigest()
    assert recomputed == record.id


# --------------------------------------------------------------------- HTTP tests
def test_post_writes_one_jsonl_line(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, server = running_daemon
    payload = _read_fixture("claude-code-turn.json")
    status, body = post_json(host, port, payload)
    assert status == 202, body
    assert body.get("ok") is True
    assert "id" in body

    server.writer_worker.flush(timeout=5.0)

    session_file = today_dir(store) / f"{payload['session_id']}.jsonl"
    assert session_file.exists(), f"missing {session_file}"
    lines = session_file.read_bytes().splitlines(keepends=True)
    assert len(lines) == 1
    assert lines[0].endswith(b"\n")
    parsed = json.loads(lines[0])
    assert parsed["session_id"] == payload["session_id"]
    assert parsed["harness"] == "claude-code"
    assert parsed["event_type"] == "stop"


def test_id_matches_sha256_of_canonical_form(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, server = running_daemon
    payload = _read_fixture("claude-code-turn.json")
    status, _ = post_json(host, port, payload)
    assert status == 202

    server.writer_worker.flush(timeout=5.0)

    session_file = today_dir(store) / f"{payload['session_id']}.jsonl"
    line = session_file.read_bytes().splitlines()[0]
    record = json.loads(line)
    recorded_id = record.pop("id")
    expected = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert recorded_id == expected


def test_concurrent_posts_same_session_serialize(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, server = running_daemon
    payload = _read_fixture("claude-code-turn.json")
    n = 20

    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker(i: int) -> None:
        try:
            local = dict(payload)
            # Vary the message so each record has a unique id.
            local["assistant_message"] = f"concurrent message {i}"
            status, _ = post_json(host, port, local)
            if status != 202:
                with lock:
                    errors.append(RuntimeError(f"unexpected status {status}"))
        except Exception as exc:  # noqa: BLE001 — test capture
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, errors

    server.writer_worker.flush(timeout=10.0)

    session_file = today_dir(store) / f"{payload['session_id']}.jsonl"
    lines = session_file.read_bytes().splitlines()
    assert len(lines) == n, f"expected {n} lines, got {len(lines)}"
    # Every line must be well-formed JSON.
    parsed_lines = [json.loads(line) for line in lines]
    assert len({p["id"] for p in parsed_lines}) == n
    # All belong to the same session.
    assert {p["session_id"] for p in parsed_lines} == {payload["session_id"]}


def test_post_to_unreachable_daemon_exits_nonzero_without_raising(
    tmp_path: Path,
) -> None:
    # Write a payload to disk and invoke the CLI client against a port nothing
    # listens on. The CLI must exit non-zero without raising into the parent.
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        (FIXTURES / "claude-code-turn.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Port 1 is reserved; loopback dial fails fast with ECONNREFUSED.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "palace.cli",
            "capture",
            "post",
            str(payload_path),
            "--port",
            "1",
            "--timeout",
            "0.5",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unreachable" in result.stderr.lower() or "warning" in result.stderr.lower()


def test_harness_and_event_type_discriminators_round_trip(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, server = running_daemon
    for fixture_name in ("claude-code-turn.json", "codex-finish.json"):
        payload = _read_fixture(fixture_name)
        status, _ = post_json(host, port, payload)
        assert status == 202

    server.writer_worker.flush(timeout=5.0)

    daily = today_dir(store)
    files = sorted(daily.iterdir())
    assert len(files) == 2
    harnesses = set()
    event_types = set()
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        harnesses.add(record["harness"])
        event_types.add(record["event_type"])
    assert harnesses == {"claude-code", "codex"}
    assert event_types == {"stop"}


def test_invalid_content_type_returns_415(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, _store, _server = running_daemon
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request(
            "POST",
            "/capture",
            body=b"not json",
            headers={"Content-Type": "text/plain"},
        )
        response = conn.getresponse()
        body = response.read()
        assert response.status == 415
        parsed = json.loads(body)
        assert "error" in parsed
    finally:
        conn.close()


def test_missing_session_id_returns_400(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, _server = running_daemon
    payload = _read_fixture("invalid-missing-session-id.json")
    status, body = post_json(host, port, payload)
    assert status == 400
    assert "session_id" in body.get("error", "")
    # And no file should have been written.
    daily = today_dir(store)
    assert not daily.exists() or not any(daily.iterdir())


def test_missing_event_type_returns_400(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, _server = running_daemon
    payload = _read_fixture("claude-code-turn.json")
    payload.pop("event_type")
    status, body = post_json(host, port, payload)
    assert status == 400
    assert "event_type" in body.get("error", "")
    daily = today_dir(store)
    assert not daily.exists() or not any(daily.iterdir())


def test_get_health_returns_ok_and_version(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, _store, _server = running_daemon
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read()
        assert response.status == 200
        parsed = json.loads(body)
        assert parsed["ok"] is True
        assert isinstance(parsed["version"], str)
        assert parsed["version"]
    finally:
        conn.close()


def test_unknown_path_returns_404(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, _store, _server = running_daemon
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request("GET", "/nope")
        response = conn.getresponse()
        body = response.read()
        assert response.status == 404
        assert "error" in json.loads(body)
    finally:
        conn.close()


def test_post_appends_to_existing_session_file(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, server = running_daemon
    payload = _read_fixture("claude-code-turn.json")
    for i in range(2):
        local = dict(payload)
        local["assistant_message"] = f"append iteration {i}"
        status, _ = post_json(host, port, local)
        assert status == 202

    server.writer_worker.flush(timeout=5.0)

    session_file = today_dir(store) / f"{payload['session_id']}.jsonl"
    lines = session_file.read_bytes().splitlines()
    assert len(lines) == 2

    # Confirm the file is genuinely append-only — second post must not have
    # truncated; both lines re-parse.
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["assistant_message"] == "append iteration 0"
    assert second["assistant_message"] == "append iteration 1"


def test_session_id_path_traversal_rejected(
    running_daemon: tuple[str, int, Path, CaptureServer],
) -> None:
    host, port, store, _server = running_daemon
    for bad in ("../escape", "a/b", "a\\b", "with\x00null"):
        payload = _read_fixture("claude-code-turn.json")
        payload["session_id"] = bad
        status, body = post_json(host, port, payload)
        assert status == 400, (bad, status, body)
        assert "session_id" in body.get("error", "")

    # No traversal path should have been created anywhere under the store.
    escaped = (store.parent / "escape.jsonl").exists()
    assert not escaped
    # ``sessions/`` must either not exist or only contain today's dir (empty).
    sessions = store / "sessions"
    if sessions.exists():
        for child in sessions.iterdir():
            assert not any(child.iterdir()), f"unexpected files under {child}"


def test_palace_cli_version_still_works() -> None:
    """Phase 0's hello-world smoke must keep passing after the CLI changes."""
    result = subprocess.run(
        [sys.executable, "-m", "palace.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert result.returncode == 0
    assert result.stdout.startswith("palace ")
