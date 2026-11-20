"""The retired phase sketches stay exact historical records outside the live inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "collaboration" / "retained-phase-sketches.json"


def test_retired_sketches_keep_exact_bytes_reserved_ids_and_index_links() -> None:
    record = json.loads(RECORD.read_text())
    assert record["schema"] == "palace.retained-phase-sketches.v1"
    files = record["files"]
    assert sorted(files) == ["plan/phase-20.md", "plan/phase-bounded-indexing.md"]
    reserved = set()
    for relative, entry in files.items():
        path = ROOT / relative
        data = path.read_bytes()
        assert not path.is_symlink()
        assert len(data) == entry["bytes"], f"RETAINED SKETCH SIZE CHANGED: {relative}"
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], (
            f"RETAINED SKETCH BYTES CHANGED: {relative}"
        )
        assert not path.resolve().is_relative_to((ROOT / "plan" / "phases").resolve())
        reserved.add(entry["original_id"])

    phase_ids = {path.stem for path in (ROOT / "plan" / "phases").glob("*.md")}
    events = [json.loads(path.read_text()) for path in (ROOT / "plan" / "events").glob("*.json")]
    imported = {event.get("source_phase") for event in events}
    mapping = json.loads((ROOT / "collaboration" / "manifest.json").read_text())["phase_mapping"]
    assert not reserved & (phase_ids | imported | set(mapping)), "RESERVED SKETCH ID REUSED"
    assert record["successor"]["phase"] in phase_ids
    assert mapping[record["successor"]["source_phase"]] == record["successor"]["phase"]

    index = (ROOT / "plan" / "INDEX.md").read_text()
    for relative in files:
        assert f"]({Path(relative).name})" in index, f"INDEX LOST ITS LINK TO {relative}"
