"""Tests for the fact id computer and section serializer / parser."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

import yaml

from palace.fact.schema import (
    Fact,
    build_fact,
    fact_id,
    parse_fact,
    patch_superseded_by,
    patch_valid,
    serialize_fact,
)


def _json_safe(value: Any) -> Any:
    """Coerce YAML date/datetime scalars to ISO strings for JSON dumping."""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def independent_id(section_text: str) -> str:
    """Recompute a section's id from scratch, per the binding recipe.

    (a) ``yaml.safe_load`` the frontmatter; (b) project exactly the seven
    keys ``{event_time, ingest_time, confidence, provenance, tags, refs,
    claim}`` with ``claim`` taken from the section BODY; (c) default
    ``tags`` / ``refs`` to ``[]`` when absent; (d) coerce YAML-parsed
    date/datetime values to their ISO string form (matching how the engine
    normalizes); (e) SHA-256 over ``json.dumps(sort_keys=True,
    separators=(",", ":"))`` — the exact args of ``compute_record_id``
    (which relies on ``ensure_ascii=True``, the Python default).
    """
    lines = section_text.split("\n")
    fences = [i for i, line in enumerate(lines) if line == "---"]
    frontmatter = "\n".join(lines[fences[0] + 1 : fences[1]])
    data = yaml.safe_load(frontmatter)

    body_lines = lines[fences[1] + 1 :]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    claim = "\n".join(body_lines).rstrip("\n")

    obj = {
        "event_time": _json_safe(data["event_time"]),
        "ingest_time": _json_safe(data["ingest_time"]),
        "confidence": data["confidence"],
        "provenance": data.get("provenance") or [],
        "tags": data.get("tags") or [],
        "refs": data.get("refs") or [],
        "claim": claim,
    }
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sample_scalar() -> Fact:
    return build_fact(
        summary="Scalar event time",
        claim="A claim with a scalar event time.",
        event_time="2026-01-15",
        ingest_time="2026-05-17T09:00:00-06:00",
        confidence=0.9,
        provenance=["event:abc123"],
        tags=["decisions"],
        refs=["[[simulation-guide]]"],
    )


def _sample_interval() -> Fact:
    return build_fact(
        summary="Interval event time",
        claim="A claim with an open-ended interval.",
        event_time={"start": "2026-04-01", "end": None},
        ingest_time="2026-05-17T09:00:00-06:00",
        confidence=0.82,
        provenance=["event:def456", "event:ghi789"],
        tags=["juniper-observatory", "infrastructure"],
        refs=["[[training-project/architecture]]"],
    )


def test_fact_id_matches_independent_recompute_scalar() -> None:
    fact = _sample_scalar()
    assert independent_id(serialize_fact(fact)) == fact.id


def test_fact_id_matches_independent_recompute_interval() -> None:
    fact = _sample_interval()
    assert independent_id(serialize_fact(fact)) == fact.id


def test_fact_id_normalizes_tags_refs_to_empty_list() -> None:
    no_tags = fact_id(
        event_time="2026-01-15",
        ingest_time="2026-05-17T09:00:00-06:00",
        confidence=1.0,
        provenance=["e"],
        tags=(),
        refs=(),
        claim="c",
    )
    explicit_empty = fact_id(
        event_time="2026-01-15",
        ingest_time="2026-05-17T09:00:00-06:00",
        confidence=1.0,
        provenance=["e"],
        tags=[],
        refs=[],
        claim="c",
    )
    assert no_tags == explicit_empty


def test_valid_toggle_does_not_change_id() -> None:
    fact = _sample_scalar()
    section = serialize_fact(fact)
    patched = patch_valid(section, False)
    assert parse_fact(patched).id == fact.id


def test_superseded_by_toggle_does_not_change_id() -> None:
    fact = _sample_interval()
    section = serialize_fact(fact)
    patched = patch_superseded_by(section, "deadbeef" * 8)
    assert parse_fact(patched).id == fact.id


def test_serializer_field_order() -> None:
    fact = _sample_interval()
    section = serialize_fact(fact)
    keys = [
        line.split(":", 1)[0]
        for line in section.split("\n")
        if line and not line.startswith(" ") and not line.startswith("#") and ":" in line
    ]
    # Frontmatter keys appear in the policy-mandated order. event_time is a
    # block (its 'start'/'end' lines are indented and excluded above).
    assert keys[:8] == [
        "id",
        "event_time",
        "ingest_time",
        "confidence",
        "valid",
        "provenance",
        "tags",
        "refs",
    ]


def test_serializer_superseded_by_position() -> None:
    fact = _sample_scalar()
    patched = patch_superseded_by(serialize_fact(fact), "f" * 64)
    lines = patched.split("\n")
    valid_idx = next(i for i, line in enumerate(lines) if line.startswith("valid:"))
    superseded_idx = next(i for i, line in enumerate(lines) if line.startswith("superseded_by:"))
    assert superseded_idx == valid_idx + 1


def test_tags_omitted_when_empty() -> None:
    fact = build_fact(
        summary="No tags or refs",
        claim="Bare claim.",
        event_time="2026-01-15",
        ingest_time="2026-05-17T09:00:00-06:00",
        confidence=1.0,
        provenance=["event:x"],
    )
    section = serialize_fact(fact)
    assert "tags:" not in section
    assert "refs:" not in section


def test_parse_round_trips_fact_ignoring_raw() -> None:
    for fact in (_sample_scalar(), _sample_interval()):
        parsed = parse_fact(serialize_fact(fact))
        assert parsed.id == fact.id
        assert parsed.summary == fact.summary
        assert parsed.claim == fact.claim
        assert parsed.event_time == fact.event_time
        assert parsed.ingest_time == fact.ingest_time
        assert parsed.confidence == fact.confidence
        assert parsed.valid == fact.valid
        assert parsed.provenance == fact.provenance
        assert parsed.tags == fact.tags
        assert parsed.refs == fact.refs
        assert parsed.superseded_by == fact.superseded_by


def test_serialize_fact_matches_policy_example_shape() -> None:
    """The serializer emits the exact shape of the policy's worked example."""
    fact = build_fact(
        summary="Juniper Observatory adopted sqlite-vec after evaluating alternatives",
        claim="Juniper Observatory adopted sqlite-vec on 2026-04-01.",
        event_time={"start": "2026-04-01", "end": None},
        ingest_time="2026-05-17T14:33:00-06:00",
        confidence=0.82,
        provenance=["event:b7e3a1c4", "event:5c01ff9d"],
        tags=["juniper-observatory", "infrastructure", "decisions"],
        refs=["[[training-project/architecture]]", "[[sqlite-vec]]"],
    )
    section = serialize_fact(fact)
    assert "event_time:\n  start: 2026-04-01\n  end: null\n" in section
    assert "tags: [juniper-observatory, infrastructure, decisions]\n" in section
    assert '  - "[[training-project/architecture]]"\n' in section
