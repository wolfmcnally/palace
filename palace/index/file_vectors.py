"""Bounded observations of one committed file, under the caller's writer lock."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import asdict, dataclass, fields
from pathlib import Path, PurePosixPath

from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError
from palace.index.config import EmbeddingIdentity
from palace.index.enrich import scored_text
from palace.index.representation import (
    has_input_evidence,
    input_digest,
    parse_representation,
    validate_input_digest,
)
from palace.index.schema import (
    ChunkRecord,
    FileRecord,
    assert_identity,
    assert_identity_readable,
    compute_chunk_id,
)


@dataclass(frozen=True, slots=True)
class CommittedChunkVector:
    """A committed chunk, its float32 vector, and its exact UTF-8 input digest."""

    chunk: ChunkRecord
    vector: tuple[float, ...]
    input_sha256: str


@dataclass(frozen=True, slots=True)
class FileVectorSnapshot:
    """One nominated file view; not a reservation, authorization or store checkpoint."""

    file: FileRecord
    identity: EmbeddingIdentity
    chunks: tuple[CommittedChunkVector, ...]
    view_sha256: str


_CHUNK_COLUMNS = tuple(field.name for field in fields(ChunkRecord))
_FILE_COLUMNS = tuple(field.name for field in fields(FileRecord))
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_MAX_CHUNKS = 16384


def _byte_lengths(columns: tuple[str, ...]) -> str:
    return "+".join(f"coalesce(length(cast({name} AS BLOB)),0)" for name in columns)


def _chunk_identity(chunk: ChunkRecord) -> str:
    if chunk.kind == "jsonl" and chunk.frontmatter_json is not None:
        try:
            frontmatter = json.loads(chunk.frontmatter_json)
        except ValueError as exc:
            raise IndexError("invalid committed JSONL identity evidence") from exc
        if isinstance(frontmatter, dict) and frontmatter.get("jsonl_record_id") is not None:
            return compute_record_id({"jsonl_record_id": frontmatter["jsonl_record_id"]})
    return compute_chunk_id(
        watch_root=chunk.watch_root,
        path=chunk.path,
        section_index=chunk.section_index,
        window_index=chunk.window_index,
        body_hash=chunk.body_hash,
    )


@dataclass(frozen=True, slots=True)
class FileVectorLimits:
    """Finite encoded snapshot budget; not a bound on RSS or database size."""

    max_bytes: int = _DEFAULT_MAX_BYTES
    max_chunks: int = _MAX_CHUNKS

    def __post_init__(self) -> None:
        if any(
            isinstance(n, bool) or not isinstance(n, int) or n <= 0
            for n in (self.max_chunks, self.max_bytes)
        ):
            raise IndexError("file vector limits must be positive integers")
        if self.max_chunks > _MAX_CHUNKS:
            raise IndexError("file vector limits exceed the supported maximum")


def _validate_file_vector_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or PurePosixPath(path).is_absolute()
        or PurePosixPath(path).as_posix() != path
        or ".." in PurePosixPath(path).parts
        or path == "."
        or "\\" in path
        or "\x00" in path
    ):
        raise IndexError("file vector path must be a normalized relative POSIX file path")


def _check_file_vector_bounds(
    *,
    conn: sqlite3.Connection,
    watch_root: str | Path,
    path: str,
    expected_identity: EmbeddingIdentity,
    limits: FileVectorLimits,
) -> bool:
    """Measure the exact encoded payload in SQLite, without fetching it.

    Internal writers may call this on their staged transaction. Public readers
    require an idle connection. True means a file exists and fits the budget;
    payload integrity remains the reader's responsibility.
    """
    max_bytes, max_chunks = limits.max_bytes, limits.max_chunks
    # Bound identity and schema text before the shared identity validators read it.
    identity_size = 0
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_meta'"
    ).fetchone():
        identity_size = conn.execute(
            "SELECT coalesce(sum(length(cast(key AS BLOB))+length(cast(value AS BLOB))),0) "
            "FROM index_meta WHERE key IN "
            "('embed_convention','embed_model','embed_provider','embed_dim')"
        ).fetchone()[0]
    identity_size += conn.execute(
        "SELECT coalesce(sum(length(cast(sql AS BLOB))),0) FROM sqlite_master "
        "WHERE name='chunks_vec'"
    ).fetchone()[0]
    if identity_size > max_bytes:
        raise IndexError("file vector snapshot exceeds the requested bounds")
    assert_identity_readable(conn)
    assert_identity(conn, expected_identity)
    supplied = parse_representation(expected_identity.convention) is not None
    key = (str(watch_root), path)
    file_size = conn.execute(
        f"SELECT {_byte_lengths(_FILE_COLUMNS)} FROM files WHERE watch_root=? AND path=?", key
    ).fetchone()
    count = conn.execute(
        "SELECT count(*) FROM (SELECT 1 FROM chunks WHERE watch_root=? AND path=? LIMIT ?)",
        (*key, max_chunks + 1),
    ).fetchone()[0]
    if file_size is None:
        if count:
            raise IndexError("orphan committed chunks have no file record")
        return False
    if count > max_chunks or identity_size + file_size[0] > max_bytes:
        raise IndexError("file vector snapshot exceeds the requested bounds")
    if supplied and not has_input_evidence(conn):
        raise IndexError("missing committed embedding input evidence")
    # Measure in SQLite before fetching any text/vector payload. Both subqueries
    # use their chunk primary keys; the outer query nominates only this file.
    vector_size = (
        "coalesce((SELECT length(embedding) FROM chunks_vec v WHERE v.chunk_id=c.chunk_id),0)"
    )
    evidence_size = (
        "coalesce((SELECT length(cast(input_sha256 AS BLOB)) FROM chunk_embedding_inputs i "
        "WHERE i.chunk_id=c.chunk_id),0)"
        if supplied
        else "0"
    )
    byte_count = conn.execute(
        f"SELECT coalesce(sum({_byte_lengths(_CHUNK_COLUMNS)}+{vector_size}+{evidence_size}),0) "
        "FROM chunks c WHERE watch_root=? AND path=?",
        key,
    ).fetchone()[0]
    if identity_size + file_size[0] + byte_count > max_bytes:
        raise IndexError("file vector snapshot exceeds the requested bounds")
    return True


def read_file_vectors(
    *,
    conn: sqlite3.Connection,
    watch_root: str | Path,
    path: str,
    expected_identity: EmbeddingIdentity,
    max_chunks: int = _MAX_CHUNKS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> FileVectorSnapshot | None:
    """Read exactly one committed file, refusing malformed or oversized evidence.

    Hold the same-store writer_lock before opening this idle connection until
    after planning, embedding, publication and connection close, on one writer
    thread. This reader never locks, mutates, commits or rolls back. After any
    lock release, reread and revalidate before using vectors. Limits count rows
    and encoded database payload bytes (not Python object overhead), and refuse
    rather than truncate. No provider or caller eligibility check is performed.
    """
    if conn.in_transaction:
        raise IndexError("read_file_vectors requires an idle connection, not an active transaction")
    limits = FileVectorLimits(max_bytes=max_bytes, max_chunks=max_chunks)
    _validate_file_vector_path(path)
    if not _check_file_vector_bounds(
        conn=conn,
        watch_root=watch_root,
        path=path,
        expected_identity=expected_identity,
        limits=limits,
    ):
        return None
    supplied = parse_representation(expected_identity.convention) is not None
    key = (str(watch_root), path)
    file = FileRecord(
        *conn.execute(
            f"SELECT {','.join(_FILE_COLUMNS)} FROM files WHERE watch_root=? AND path=?", key
        ).fetchone()
    )
    records = conn.execute(
        f"SELECT {','.join(_CHUNK_COLUMNS)} FROM chunks WHERE watch_root=? AND path=? "
        "ORDER BY section_index, window_index, chunk_id",
        key,
    ).fetchall()
    manifest = []
    positions = set()
    result = []
    vector_hashes = []
    for record in records:
        chunk = ChunkRecord(*record)
        position = (chunk.section_index, chunk.window_index)
        if (
            position in positions
            or min(position) < 0
            or chunk.kind != file.kind
            or input_digest(chunk.body) != chunk.body_hash
            or _chunk_identity(chunk) != chunk.chunk_id
        ):
            raise IndexError("committed chunk identity or body integrity mismatch")
        positions.add(position)
        manifest.append([*position, chunk.body_hash, chunk.chunk_id])
        held = conn.execute(
            "SELECT embedding FROM chunks_vec WHERE chunk_id=?", (chunk.chunk_id,)
        ).fetchone()
        if (
            held is None
            or not isinstance(held[0], bytes)
            or len(held[0]) != 4 * expected_identity.dim
        ):
            raise IndexError("missing or wrong-width committed vector")
        blob = held[0]
        vector = struct.unpack(f"{expected_identity.dim}f", blob)
        if any(not math.isfinite(value) for value in vector):
            raise IndexError("non-finite committed vector")
        if supplied:
            evidence = conn.execute(
                "SELECT input_sha256 FROM chunk_embedding_inputs WHERE chunk_id=?",
                (chunk.chunk_id,),
            ).fetchone()
            digest = validate_input_digest(None if evidence is None else evidence[0])
        else:
            digest = input_digest(
                scored_text(path=chunk.path, heading=chunk.heading, body=chunk.body)
            )
        result.append(CommittedChunkVector(chunk, vector, digest))
        vector_hashes.append(hashlib.sha256(blob).hexdigest())
    try:
        recorded_manifest = json.loads(file.sections_json)
    except ValueError as exc:
        raise IndexError("invalid committed file manifest") from exc
    if recorded_manifest != manifest:
        raise IndexError("committed file manifest does not match its ordered chunks")
    view = {
        "file": asdict(file),
        "identity": asdict(expected_identity),
        "chunks": [
            {"chunk": asdict(row.chunk), "input_sha256": row.input_sha256, "vector_sha256": digest}
            for row, digest in zip(result, vector_hashes, strict=True)
        ],
    }
    return FileVectorSnapshot(file, expected_identity, tuple(result), compute_record_id(view))
