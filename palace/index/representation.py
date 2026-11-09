"""Explicit model-input conventions and their per-vector evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass

from palace.index._errors import IndexError
from palace.index.config import EMBED_CONVENTION

_PREFIX = "supplied-inputs-v1:"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
INPUTS_DDL = (
    "CREATE TABLE IF NOT EXISTS chunk_embedding_inputs "
    "(chunk_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL)"
)


@dataclass(frozen=True, slots=True)
class EmbeddingRepresentation:
    """Caller-declared mapping, implementation, chunker and filename policy.

    Each label is nonblank and at most 256 UTF-8 bytes. Equality is a necessary
    condition for reuse, never proof of equal actual inputs or model behavior.
    """

    convention: str
    implementation: str
    chunker: str
    name_policy: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > 256:
                raise IndexError("representation labels must be nonblank, at most 256 UTF-8 bytes")

    @property
    def marker(self) -> str:
        """Canonical value of EmbeddingIdentity.convention for this representation."""
        return _PREFIX + json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def parse_representation(convention: str | None) -> EmbeddingRepresentation | None:
    """Recognize exactly the default or a canonical explicit descriptor; refuse others."""
    if convention == EMBED_CONVENTION:
        return None
    if (
        not isinstance(convention, str)
        or len(convention) > 8192
        or not convention.startswith(_PREFIX)
    ):
        raise IndexError("unsupported embedding representation")
    try:
        value = json.loads(convention[len(_PREFIX) :])
        if not isinstance(value, dict):
            raise ValueError
        result = EmbeddingRepresentation(**value)
        if result.marker != convention:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise IndexError("invalid embedding representation descriptor") from exc
    return result


def require_default_inputs(convention: str) -> None:
    if parse_representation(convention) is not None:
        raise IndexError(
            "explicit embedding representation requires plan_one/with_embedding_inputs/commit_plan"
        )


def input_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_input_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise IndexError("missing or invalid committed embedding input digest")
    return value


def has_input_evidence(conn: sqlite3.Connection, *, source: bool = False) -> bool:
    schema = "source" if source else "main"
    return (
        conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master "
            "WHERE type='table' AND name='chunk_embedding_inputs'"
        ).fetchone()
        is not None
    )


def delete_input_evidence(conn: sqlite3.Connection, chunk_ids: list[str] | tuple[str, ...]) -> None:
    # Default representations need no auxiliary evidence, including in stores
    # never opened by the explicit-input writer.
    if chunk_ids and has_input_evidence(conn):
        conn.executemany(
            "DELETE FROM chunk_embedding_inputs WHERE chunk_id = ?", ((cid,) for cid in chunk_ids)
        )
