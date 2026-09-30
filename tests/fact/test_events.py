"""Tests for the fact-lifecycle event records and their durable appender."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from palace.fact.events import (
    EVENT_TYPE_FACT_INVALIDATE,
    EVENT_TYPE_FACT_SUPERSEDE,
    append_fact_event,
    build_fact_event,
)


def _recompute_id(record: dict[str, object]) -> str:
    body = {k: v for k, v in record.items() if k != "id"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_invalidate_event_shape_and_id() -> None:
    event = build_fact_event(
        event_type=EVENT_TYPE_FACT_INVALIDATE,
        ingest_time="2026-06-15T12:00:00-06:00",
        fact_id="abc123",
    )
    assert set(event) == {"id", "ingest_time", "event_type", "fact_id"}
    assert event["event_type"] == "fact-invalidate"
    assert event["id"] == _recompute_id(event)


def test_supersede_event_shape_and_id() -> None:
    event = build_fact_event(
        event_type=EVENT_TYPE_FACT_SUPERSEDE,
        ingest_time="2026-06-15T12:00:00-06:00",
        old_fact_id="old1",
        new_fact_id="new1",
    )
    assert set(event) == {"id", "ingest_time", "event_type", "old_fact_id", "new_fact_id"}
    assert event["event_type"] == "fact-supersede"
    assert event["id"] == _recompute_id(event)


def test_event_lands_in_boise_day_file(store_root: Path, boise_clock: Callable[[], date]) -> None:
    event = build_fact_event(
        event_type=EVENT_TYPE_FACT_INVALIDATE,
        ingest_time="2026-06-15T12:00:00-06:00",
        fact_id="abc123",
    )
    path = append_fact_event(store_root, event, clock=boise_clock)
    assert path == store_root / "events" / "2026-06-15.jsonl"
    assert path.is_file()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded == event
    assert loaded["id"] == _recompute_id(loaded)


def test_append_is_append_only(store_root: Path, boise_clock: Callable[[], date]) -> None:
    first = build_fact_event(
        event_type=EVENT_TYPE_FACT_INVALIDATE,
        ingest_time="2026-06-15T12:00:00-06:00",
        fact_id="first",
    )
    path = append_fact_event(store_root, first, clock=boise_clock)
    first_bytes = path.read_bytes()

    second = build_fact_event(
        event_type=EVENT_TYPE_FACT_SUPERSEDE,
        ingest_time="2026-06-15T12:05:00-06:00",
        old_fact_id="first",
        new_fact_id="second",
    )
    append_fact_event(store_root, second, clock=boise_clock)
    after = path.read_bytes()

    # The first line is byte-identical after the second append.
    assert after.startswith(first_bytes)
    assert len(after.splitlines()) == 2
