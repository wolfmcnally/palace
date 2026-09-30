"""Tests for :mod:`palace.reindex.schema`."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from palace.reindex.schema import (
    EVENT_FIELDS,
    ChangeEvent,
    build_change_event,
    to_jsonl_bytes,
)


def _make_event() -> ChangeEvent:
    return build_change_event(
        ingest_time="2026-05-18T14:30:00-06:00",
        change_kind="created",
        watch_root="/tmp/watch",
        path="/tmp/watch/notes.md",
        relative_path="notes.md",
        is_directory=False,
        observed_at="2026-05-18T14:29:59-06:00",
    )


def test_change_event_id_is_deterministic() -> None:
    a = _make_event()
    b = _make_event()
    assert a.id == b.id


def test_change_event_id_ignores_key_order() -> None:
    event = _make_event()
    on_disk = json.loads(to_jsonl_bytes(event).decode("utf-8"))
    shuffled = {k: on_disk[k] for k in reversed(list(on_disk.keys()))}
    shuffled.pop("id")
    re_id = hashlib.sha256(
        json.dumps(shuffled, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert re_id == event.id


def test_change_event_id_matches_sha256_of_canonical_form() -> None:
    event = _make_event()
    line = to_jsonl_bytes(event).decode("utf-8")
    parsed = json.loads(line)
    recorded_id = parsed.pop("id")
    recomputed = hashlib.sha256(
        json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert recomputed == recorded_id


def test_change_event_serializes_to_one_line_lf_terminated() -> None:
    event = _make_event()
    raw = to_jsonl_bytes(event)
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


def test_change_event_field_set_is_complete() -> None:
    event = _make_event()
    body = asdict(event)
    assert set(body.keys()) == set(EVENT_FIELDS)
    # Absent rename fields must be ``null`` (not missing) on the on-disk record.
    assert body["rename_src_path"] is None
    assert body["rename_dest_path"] is None
