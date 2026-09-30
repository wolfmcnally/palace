"""Canonical-JSON id helper.

Every captured event carries an ``id`` equal to the SHA-256 hex of its
canonical JSON form (sorted keys, no whitespace), computed over every field
of the record **except** ``id`` itself, per
``policies/fact-schema.md`` §"Provenance and ids".

The canonical form uses Python's default ``ensure_ascii=True`` so the id
recomputes byte-for-byte from the ``python -c …`` snippet in
``plan/phase-1.1.md`` line 35 without modification.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json_bytes", "compute_record_id"]


def canonical_json_bytes(obj: Any) -> bytes:
    """Return the canonical JSON encoding of ``obj`` as UTF-8 bytes.

    Sorted keys, no whitespace, ``ensure_ascii=True`` (Python default) so
    non-ASCII characters are escaped identically to the phase-acceptance
    recompute snippet.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_record_id(record_without_id: dict[str, Any]) -> str:
    """Compute the SHA-256 hex id of a record minus the ``id`` field.

    Callers must pass a dict that does not yet contain ``id``; the result is
    stamped onto the record by the daemon before write.
    """
    if "id" in record_without_id:
        raise ValueError("compute_record_id: input must not include an 'id' field")
    return hashlib.sha256(canonical_json_bytes(record_without_id)).hexdigest()
