"""Shared parts of the prepared single- and multi-store search seams.

A prepared search is an immutable value sealed with a key drawn when this module
is imported. It binds stable store configuration (:class:`_StoreBinding`), never
corpus content. Returned-hit evidence (:class:`HitEvidence`) reports, per hit, the
exact stored bytes read in the retrieval's own read transaction, or a closed status
naming why that one entry could not be verified. Palace reports; the caller owns
whatever policy it applies to the evidence. An opt-in :class:`DistinctResults`
keeps the first eligible hit per caller-supplied key, with the caller's own key and
eligibility rule, and reports the walk in a :class:`DistinctOutcome`.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import struct
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from palace.index._errors import IndexError
from palace.index.config import EmbeddingIdentity
from palace.index.embedder_config import EmbedderConfig
from palace.index.file_vectors import committed_input_digest
from palace.index.representation import input_digest, parse_representation
from palace.index.s3vectors import (
    BACKEND_S3VECTORS,
    PublishedIndex,
    S3VectorsBackend,
    VectorBackendConfig,
)
from palace.rerank_config import RerankConfig

__all__ = ["DistinctOutcome", "DistinctResults", "HitEvidence", "HitEvidenceStatus"]

# Drawn at import: a prepared value verifies in this process and in forked children
# that inherit the key, never in a spawned or separate process.
_SEAL_KEY = secrets.token_bytes(32)


def _seal(values: tuple[object, ...]) -> str:
    message = repr(values).encode("utf-8", "backslashreplace")
    return hmac.new(_SEAL_KEY, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class _StoreBinding:
    """Stable configuration one store's prepared search depends on, never its corpus."""

    store: Path
    embedder: EmbedderConfig | None
    identity: EmbeddingIdentity | None
    vector_backend: VectorBackendConfig | None
    published_index: PublishedIndex | None
    reranker: RerankConfig | None


def _selected_backend(backend: S3VectorsBackend | None) -> VectorBackendConfig:
    """The selector a resolved backend was built from, without a second read."""
    if backend is None:
        return VectorBackendConfig()
    return VectorBackendConfig(BACKEND_S3VECTORS, backend.profile)


def _changed_axis(prepared: _StoreBinding, current: _StoreBinding) -> str | None:
    for axis, before, after in (
        ("identity", prepared.identity, current.identity),
        ("embedder", prepared.embedder, current.embedder),
        ("vector backend", prepared.vector_backend, current.vector_backend),
        ("published index", prepared.published_index, current.published_index),
        ("reranker", prepared.reranker, current.reranker),
    ):
        if before != after:
            return axis
    return None


HitEvidenceStatus = Literal[
    "verified",
    "missing-local-row",
    "body-hash-mismatch",
    "missing-vector",
    "malformed-vector",
    "missing-input-digest",
    "remote-only",
]


@dataclass(frozen=True)
class HitEvidence:
    """Per-hit evidence read in the retrieval's own snapshot of its store.

    ``watch_root``, ``path``, ``file_hash``, ``section_index`` and ``window_index``
    identify the stored chunk; they are ``None`` only when the hit has no local chunk
    row, which is ``remote-only`` for a remote vector leg and ``missing-local-row``
    otherwise (``file_hash`` is also ``None`` if the file row is absent). Only a
    ``verified`` entry carries content: the exact stored ``body``, its recorded
    ``body_hash``, the committed embedding-input digest ``input_sha256`` and the
    exact stored little-endian f32 vector bytes ``vector_f32``.
    """

    store: Path
    chunk_id: str
    status: HitEvidenceStatus
    identity: EmbeddingIdentity
    watch_root: str | None
    path: str | None
    file_hash: str | None
    section_index: int | None
    window_index: int | None
    body: str | None = None
    body_hash: str | None = None
    input_sha256: str | None = None
    vector_f32: bytes | None = None


_EVIDENCE_ROW = (
    "SELECT c.watch_root, c.path, f.file_hash, c.section_index, c.window_index, "
    "c.heading, c.body, c.body_hash "
    "FROM chunks c LEFT JOIN files f ON f.watch_root = c.watch_root AND f.path = c.path "
    "WHERE c.chunk_id = ?"
)


def _collect_hit_evidence(
    conn: sqlite3.Connection,
    *,
    store: Path,
    identity: EmbeddingIdentity,
    remote: bool,
    chunk_ids: Iterable[str],
) -> dict[str, HitEvidence]:
    """Read each hit's stored row and vector on the caller's open read transaction.

    Run it before that connection closes, so every entry comes from the snapshot the
    retrieval used. Each id costs one primary-key row read and one vector read,
    O(log n + d). The first failing check is the entry's status; a damaged entry never
    fails the call. A store whose vector leg is remote is reported, never requested.
    """
    supplied = parse_representation(identity.convention) is not None
    evidence: dict[str, HitEvidence] = {}
    for chunk_id in dict.fromkeys(chunk_ids):
        row = conn.execute(_EVIDENCE_ROW, (chunk_id,)).fetchone()
        if row is None:
            # A remote leg's match, or in a local store an orphan vector or keyword
            # posting whose chunk row is gone: no local identity exists to report.
            status: HitEvidenceStatus = "remote-only" if remote else "missing-local-row"
            evidence[chunk_id] = HitEvidence(
                store, chunk_id, status, identity, None, None, None, None, None
            )
            continue
        watch_root, path, file_hash, section_index, window_index, heading, body, body_hash = row
        located = (watch_root, path, file_hash, section_index, window_index)
        found: HitEvidenceStatus | None = "remote-only" if remote else None
        blob: object = None
        digest: str | None = None
        if found is None and input_digest(body) != body_hash:
            found = "body-hash-mismatch"
        if found is None:
            held = conn.execute(
                "SELECT embedding FROM chunks_vec WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if held is None:
                found = "missing-vector"
            else:
                blob = held[0]
        if found is None and (
            not isinstance(blob, bytes)
            or len(blob) != 4 * identity.dim
            or not all(math.isfinite(v) for v in struct.unpack(f"<{identity.dim}f", blob))
        ):
            found = "malformed-vector"
        if found is None:
            try:
                digest = committed_input_digest(
                    conn,
                    chunk_id=chunk_id,
                    path=path,
                    heading=heading,
                    body=body,
                    supplied=supplied,
                )
            except IndexError:
                found = "missing-input-digest"
        if found is not None:
            evidence[chunk_id] = HitEvidence(store, chunk_id, found, identity, *located)
            continue
        assert isinstance(blob, bytes) and digest is not None
        evidence[chunk_id] = HitEvidence(
            store, chunk_id, "verified", identity, *located, body, body_hash, digest, blob
        )
    return evidence


@dataclass(frozen=True)
class DistinctResults:
    """An opt-in request for at most ``limit`` hits, one per caller-supplied key.

    ``key(hit, evidence)`` receives each hit of the search's bounded pool in order
    with its :class:`HitEvidence` (``None`` when the search was not prepared with
    evidence) and returns the hit's group key, or ``None`` when the caller judges
    the hit ineligible. The first eligible hit per key is kept until ``limit`` are
    kept. The caller owns the key and the eligibility rule; Palace imposes neither.
    """

    key: Callable[[Any, HitEvidence | None], str | None]
    limit: int

    def __post_init__(self) -> None:
        if not callable(self.key):
            raise IndexError("distinct key must be callable")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise IndexError("distinct limit must be a positive integer")


@dataclass(frozen=True)
class DistinctOutcome:
    """How a :class:`DistinctResults` walk over the search's bounded pool went.

    ``pool`` hits were available and ``walked`` were passed to the key, in order,
    until ``returned`` hits were kept; of the walked hits ``ineligible`` had no key
    and ``duplicates`` repeated a kept key. ``underfilled`` is true when fewer than
    the requested limit were kept, which happens only after the whole pool was walked.
    """

    pool: int
    walked: int
    ineligible: int
    duplicates: int
    returned: int
    underfilled: bool


def _select_distinct[Hit](
    hits: Sequence[Hit],
    evidence: Sequence[HitEvidence] | None,
    distinct: DistinctResults,
) -> tuple[list[Hit], tuple[HitEvidence, ...] | None, DistinctOutcome]:
    """Keep the first eligible hit per key, in pool order, until the limit is kept."""
    if evidence is not None and len(evidence) != len(hits):
        raise IndexError("distinct selection received misaligned evidence")
    kept: list[int] = []
    seen: set[str] = set()
    walked = ineligible = duplicates = 0
    for index, hit in enumerate(hits):
        if len(kept) == distinct.limit:
            break
        walked += 1
        group = distinct.key(hit, evidence[index] if evidence is not None else None)
        if group is None:
            ineligible += 1
        elif not isinstance(group, str):
            raise IndexError("distinct key must return a string or None")
        elif group in seen:
            duplicates += 1
        else:
            seen.add(group)
            kept.append(index)
    outcome = DistinctOutcome(
        pool=len(hits),
        walked=walked,
        ineligible=ineligible,
        duplicates=duplicates,
        returned=len(kept),
        underfilled=len(kept) < distinct.limit,
    )
    aligned = tuple(evidence[i] for i in kept) if evidence is not None else None
    return [hits[i] for i in kept], aligned, outcome
