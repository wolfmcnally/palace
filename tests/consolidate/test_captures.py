"""Tests for the captures-directory episodic source reader.

Hermetic and offline — no live model. Covers the direct, lossless mapping of a
Markdown capture (frontmatter + verbatim body) onto a ``SourceUnit``, the
source-trust predicate, the filename-skip vs. malformed-raise distinction, the
``--since`` filter, and sort-by-source_id determinism.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.captures import (
    parse_capture,
    read_captures_dir,
    source_is_user_authored,
)

_USER_ID = "67cb5bccc877d880d63429d78e975368ec7270e9b44c3005625c22f875ccbfb1"
_JOURNAL_ID = "828d61a5087c4197a41d240c85cc26b7674d4bb56390499c686c33cb0f8a9b87"


def _capture(
    *,
    cid: str,
    source: str,
    body: str,
    event_time: str = "2026-06-14T10:00:00+00:00",
    ingest_time: str = "2026-06-14T10:00:00+00:00",
) -> str:
    return (
        "---\n"
        f"id: {cid}\n"
        f"event_time: {event_time}\n"
        f"ingest_time: {ingest_time}\n"
        f'source: "{source}"\n'
        "tags: [t]\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


def test_parse_maps_frontmatter_and_verbatim_body() -> None:
    body = "First line of the claim.\n\nA second paragraph, kept verbatim."
    text = _capture(cid=_USER_ID, source="user:simulated-operator", body=body)
    unit = parse_capture(text, path=Path("user-cap-67cb5bccc877.md"))

    assert unit.source_id == _USER_ID
    # Body verbatim, including internal blank line; trailing newline stripped.
    assert unit.text == body
    assert unit.event_time == "2026-06-14T10:00:00+00:00"
    assert unit.ingest_time == "2026-06-14T10:00:00+00:00"
    assert unit.harness is None
    assert unit.authored is True


def test_date_frontmatter_normalized_to_iso_string() -> None:
    # A bare YAML date (no time/offset) decodes to a datetime.date; it must
    # normalize to the ISO string form, not a Python repr.
    text = _capture(
        cid=_USER_ID,
        source="user:simulated-operator",
        body="claim",
        event_time="2026-06-14",
        ingest_time="2026-06-14",
    )
    unit = parse_capture(text, path=Path("user-cap-67cb5bccc877.md"))
    assert unit.event_time == "2026-06-14"
    assert unit.ingest_time == "2026-06-14"


def test_authored_false_for_journal_and_missing_source() -> None:
    assert source_is_user_authored("user:simulated-operator-statement") is True
    assert source_is_user_authored("journal:entry-42") is False
    assert source_is_user_authored(None) is False
    assert source_is_user_authored("") is False


def test_non_matching_filename_skipped(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    # A valid capture and an unrelated Markdown file (no <slug>-<12hex>.md shape).
    (captures / f"user-cap-{_USER_ID[:12]}.md").write_text(
        _capture(cid=_USER_ID, source="user:simulated-operator", body="claim"), encoding="utf-8"
    )
    (captures / "NOTES.md").write_text("# just notes\n", encoding="utf-8")

    units = read_captures_dir(captures)
    assert len(units) == 1
    assert units[0].source_id == _USER_ID


def test_malformed_frontmatter_raises_with_path(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    bad = captures / f"bad-cap-{_USER_ID[:12]}.md"
    # A matching filename but no closing frontmatter fence: genuinely malformed.
    bad.write_text("---\nid: " + _USER_ID + "\nclaim body with no close fence\n", encoding="utf-8")

    with pytest.raises(ConsolidationError) as excinfo:
        read_captures_dir(captures)
    assert str(bad) in str(excinfo.value)


def test_missing_or_invalid_id_raises(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    bad = captures / f"bad-cap-{_USER_ID[:12]}.md"
    bad.write_text(
        "---\nid: not-a-64-hex-id\nevent_time: 2026-06-14\n"
        "ingest_time: 2026-06-14\nsource: user:x\n---\n\nclaim\n",
        encoding="utf-8",
    )
    with pytest.raises(ConsolidationError) as excinfo:
        read_captures_dir(captures)
    assert str(bad) in str(excinfo.value)


def test_since_filters_by_event_time(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / f"old-cap-{_USER_ID[:12]}.md").write_text(
        _capture(
            cid=_USER_ID, source="user:simulated-operator", body="old", event_time="2026-06-10"
        ),
        encoding="utf-8",
    )
    (captures / f"new-cap-{_JOURNAL_ID[:12]}.md").write_text(
        _capture(
            cid=_JOURNAL_ID, source="user:simulated-operator", body="new", event_time="2026-06-20"
        ),
        encoding="utf-8",
    )

    units = read_captures_dir(captures, since=date(2026, 6, 15))
    assert [u.source_id for u in units] == [_JOURNAL_ID]


def test_sorted_by_source_id(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / f"j-cap-{_JOURNAL_ID[:12]}.md").write_text(
        _capture(cid=_JOURNAL_ID, source="journal:x", body="b"), encoding="utf-8"
    )
    (captures / f"u-cap-{_USER_ID[:12]}.md").write_text(
        _capture(cid=_USER_ID, source="user:x", body="a"), encoding="utf-8"
    )
    units = read_captures_dir(captures)
    # _USER_ID (244...) sorts before _JOURNAL_ID (47a...).
    assert [u.source_id for u in units] == sorted([_USER_ID, _JOURNAL_ID])


def test_missing_directory_is_empty(tmp_path: Path) -> None:
    assert read_captures_dir(tmp_path / "nope") == []
