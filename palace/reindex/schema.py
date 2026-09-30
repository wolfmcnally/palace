"""Change-event record dataclass and canonical serializer.

One JSON object per line in ``<store>/events/<YYYY-MM-DD>.jsonl``, UTF-8,
LF-terminated, sorted-key form on disk so the canonical-id idiom from
Phase 1.1 recomputes byte-for-byte against any line:

    python -c "import json,hashlib,sys; \\
        d=json.loads(sys.stdin.read()); d.pop('id'); \\
        print(hashlib.sha256(json.dumps(d,sort_keys=True,\\
        separators=(',',':')).encode()).hexdigest())"

The ``id`` helper reuses :func:`palace.daemons.capture.ids.compute_record_id`
verbatim — no second SHA-256 implementation. The dataclass is frozen so a
constructed event cannot be mutated between ``id`` computation and write.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from palace.daemons.capture.ids import compute_record_id
from palace.reindex.config import EVENT_TYPE_FS_CHANGE, ChangeKind

__all__ = [
    "EVENT_FIELDS",
    "ChangeEvent",
    "build_change_event",
    "to_jsonl_bytes",
]


# The canonical, ordered field set for every ``ChangeEvent``. Listed here
# so tests can pin "every field is always present (absent values are
# ``null``)" without re-reading the dataclass definition. ``id`` leads
# because that is how the on-disk dict reads; everything else follows in
# the order the dataclass declares.
EVENT_FIELDS: tuple[str, ...] = (
    "id",
    "ingest_time",
    "event_type",
    "change_kind",
    "watch_root",
    "path",
    "relative_path",
    "is_directory",
    "rename_src_path",
    "rename_dest_path",
    "observed_at",
)


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One filesystem change observed by the reindex daemon.

    Field shape exactly mirrors ``plan/phase-2.2.md`` lines 45-58. The
    daemon stamps ``id``, ``ingest_time``, and ``event_type``; the observer
    fills the remaining fields. Rename events emit two ``ChangeEvent``
    records (one ``"deleted"`` carrying ``rename_src_path``, one
    ``"created"`` carrying ``rename_dest_path``) so downstream consumers
    never need to special-case moves.
    """

    id: str
    ingest_time: str
    event_type: str
    change_kind: ChangeKind
    watch_root: str
    path: str
    relative_path: str
    is_directory: bool
    rename_src_path: str | None
    rename_dest_path: str | None
    observed_at: str


def build_change_event(
    *,
    ingest_time: str,
    change_kind: ChangeKind,
    watch_root: str,
    path: str,
    relative_path: str,
    is_directory: bool,
    observed_at: str,
    rename_src_path: str | None = None,
    rename_dest_path: str | None = None,
) -> ChangeEvent:
    """Build a ``ChangeEvent``, stamping ``id`` from canonical JSON.

    Assembles the body dict deliberately without an ``id`` field, hands it
    to :func:`compute_record_id` (which raises if ``id`` is present), then
    constructs the frozen dataclass.
    """
    body: dict[str, Any] = {
        "ingest_time": ingest_time,
        "event_type": EVENT_TYPE_FS_CHANGE,
        "change_kind": change_kind,
        "watch_root": watch_root,
        "path": path,
        "relative_path": relative_path,
        "is_directory": is_directory,
        "rename_src_path": rename_src_path,
        "rename_dest_path": rename_dest_path,
        "observed_at": observed_at,
    }
    record_id = compute_record_id(body)
    return ChangeEvent(id=record_id, **body)


def to_jsonl_bytes(event: ChangeEvent) -> bytes:
    """Serialize ``event`` as one canonical-JSON line, LF-terminated.

    Sorted keys, no whitespace, UTF-8 — matches
    ``policies/storage-layout.md`` §5 and the capture daemon's wire
    format, so the recompute-from-jq snippet documented at the top of this
    module works against any emitted line.
    """
    line = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
    return (line + "\n").encode("utf-8")
