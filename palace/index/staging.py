"""Private, bounded acceleration artifacts; never a source of canonical facts."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, overload

from palace.index._errors import IndexError
from palace.index.core import FilePlan, IndexOutcome, PendingFileRow, PendingRow

_INDEX = struct.Struct("<QQ32s")
_CONTRACT = "palace-build-turns-1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any, cap: int) -> bytes:
    """Stop serialization before accumulating more than the declared plan budget."""
    output = io.BytesIO()
    for piece in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).iterencode(value):
        part = piece.encode("utf-8")
        if output.tell() + len(part) > cap:
            raise IndexError(
                f"build serialized-plan limit exceeded ({cap} bytes); "
                "ignore the source or adjust plan_bytes through Python BuildLimits"
            )
        output.write(part)
    return output.getvalue()


@dataclass(frozen=True)
class SourceBinding:
    root: str
    path: str
    offset: int
    length: int
    sha256: str
    append_only: bool

    def read(self, cap: int) -> bytes:
        target = Path(self.root) / self.path
        try:
            with target.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(min(self.length, cap) + 1)
        except FileNotFoundError as exc:
            raise IndexError(
                f"source vanished before publication: {self.path}; rerun the build"
            ) from exc
        if self.append_only:
            data = data[: self.length]
        if len(data) != self.length or digest(data) != self.sha256:
            raise IndexError(f"source changed before publication: {self.path}; rerun the build")
        return data


@dataclass(frozen=True)
class Prepared:
    key: str
    source: SourceBinding
    count: int
    reservation: int
    plan_hash: str
    plan_size: int
    small: bool
    index_hash: str


class Staging:
    """All methods run under the selected store's existing writer lock."""

    def __init__(
        self,
        *,
        store: Path,
        budget: int,
        plan_cap: int,
        dim: int,
        identity: dict[str, Any],
        partition: tuple[int, int],
        log: Callable[[str], None],
    ) -> None:
        self.root = store / "index" / "build-staging"
        for parent in (self.root.parent, self.root):
            if parent.is_symlink():
                raise IndexError("build staging refuses symlinks")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.budget, self.plan_cap, self.dim = budget, plan_cap, dim
        self.identity, self.partition, self.log = identity, partition, log
        self.reservations: dict[str, int] = {}
        self._sizes: dict[str, int] = {}
        self._scan()

    def _scan(self) -> None:
        sizes: dict[str, int] = {}
        entries = 0
        for directory, directories, files in os.walk(self.root, followlinks=False):
            for name in (*directories, *files):
                target = Path(directory) / name
                entries += 1
                if entries > 1_000_000 or target.is_symlink():
                    raise IndexError(
                        "build staging has excessive entries or a symlink; inspect derived staging"
                    )
                if target.is_file():
                    key = target.relative_to(self.root).parts[0]
                    sizes[key] = sizes.get(key, 0) + target.stat().st_size
        self._sizes = sizes
        self.total_bytes = sum(sizes.values())
        if self.total_bytes > self.budget:
            raise IndexError(
                "existing build staging exceeds staging_bytes; "
                "restore the previous budget or remove derived staging"
            )

    def prepare(
        self, plan: FilePlan, source: SourceBinding, *, small: bool
    ) -> tuple[Prepared, bytes]:
        body = encoded(asdict(plan), self.plan_cap)
        index = io.BytesIO()
        offset = 0
        for text, chunk_id in zip(plan.embed_inputs, plan.chunk_ids, strict=True):
            line = encoded({"input": text, "chunk_id": chunk_id}, self.plan_cap) + b"\n"
            index.write(_INDEX.pack(offset, len(line), hashlib.sha256(line).digest()))
            offset += len(line)
        index_hash = digest(index.getvalue())
        binding = {
            "contract": _CONTRACT,
            "source": asdict(source),
            "identity": self.identity,
            "partition": self.partition,
            "plan_sha256": digest(body),
            "input_index": index_hash,
        }
        key = digest(encoded(binding, self.plan_cap))
        # Includes seekable inputs, indexes, receipts and temporary completion writes.
        reservation = 2 * len(body) + len(plan.rows) * (self.dim * 4 + 16384) + 16384
        return Prepared(
            key, source, len(plan.rows), reservation, digest(body), len(body), small, index_hash
        ), body

    def gc(self, valid: set[str], selected: set[str]) -> int:
        reclaimed = 0
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                raise IndexError("unexpected file in build staging; inspect derived staging")
            metadata = entry / "binding.json"
            if not metadata.exists():
                # An interrupted initial write has no reusable completion proof.
                remove = True
            else:
                data = self._json(metadata, 16384)
                if (
                    set(data) != {"identity", "source", "plan_hash"}
                    or not isinstance(data["source"], dict)
                    or not isinstance(data["source"].get("root"), str)
                    or not isinstance(data["plan_hash"], str)
                ):
                    raise IndexError("invalid build-staging binding")
                remove = data.get("identity") != self.identity or (
                    data.get("source", {}).get("root") in selected and entry.name not in valid
                )
            if remove:
                reclaimed += self._sizes.get(entry.name, 0)
                self._remove(entry)
        self._scan()
        if reclaimed:
            self.log(f"palace index progress: event=reclaimed bytes={reclaimed}")
        return reclaimed

    def _remove(self, directory: Path) -> None:
        if directory.parent != self.root or directory.is_symlink():
            raise IndexError("unsafe build-staging removal refused")
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_file():
                raise IndexError("unexpected nested build-staging entry")
        shutil.rmtree(directory)

    def _load(self) -> int:
        return self.total_bytes + sum(
            max(0, value - self._sizes.get(key, 0)) for key, value in self.reservations.items()
        )

    def reservation_ceiling(self, ref: Prepared, small_reserve: int) -> int:
        ceiling = self.budget if ref.small else self.budget - small_reserve
        if ref.reservation > ceiling:
            raise IndexError(
                f"document staging reservation exceeds supported budget: {ref.source.path}; "
                "ignore it or adjust staging_bytes through Python BuildLimits"
            )
        return ceiling

    def can_admit(self, ref: Prepared, small_reserve: int) -> bool:
        ceiling = self.reservation_ceiling(ref, small_reserve)
        return self._load() + max(0, ref.reservation - self._sizes.get(ref.key, 0)) <= ceiling

    def admit(self, ref: Prepared, body: bytes, plan: FilePlan) -> None:
        self.reservations[ref.key] = ref.reservation
        directory = self.root / ref.key
        if directory.is_symlink():
            raise IndexError("build staging refuses symlinks")
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = {
            "identity": self.identity,
            "source": asdict(ref.source),
            "plan_hash": ref.plan_hash,
        }
        self._atomic(directory / "binding.json", encoded(metadata, 16384))
        target = directory / "plan.json"
        if target.exists():
            if digest(self._bytes(target, self.plan_cap)) != ref.plan_hash:
                raise IndexError("corrupt staged plan; remove derived staging and rerun")
            return
        inputs, offsets = io.BytesIO(), io.BytesIO()
        for text, chunk_id in zip(plan.embed_inputs, plan.chunk_ids, strict=True):
            line = encoded({"input": text, "chunk_id": chunk_id}, self.plan_cap) + b"\n"
            offsets.write(_INDEX.pack(inputs.tell(), len(line), hashlib.sha256(line).digest()))
            inputs.write(line)
            if inputs.tell() + offsets.tell() > self.plan_cap:
                raise IndexError("seekable build input budget exceeded")
        self._atomic(directory / "inputs.jsonl", inputs.getvalue())
        self._atomic(directory / "inputs.idx", offsets.getvalue())
        self._atomic(target, body)

    def _atomic(self, target: Path, data: bytes) -> None:
        if target.is_symlink() or target.parent.is_symlink():
            raise IndexError("build staging refuses symlinks")
        # Reservations cover this write including a simultaneously present old file.
        old_size = target.stat().st_size if target.exists() else 0
        if self.total_bytes + len(data) > self.budget:
            raise IndexError(
                "build staging byte limit exhausted; inspect/remove derived index/build-staging "
                "or adjust staging_bytes through Python BuildLimits"
            )
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
        self._sizes[target.parent.name] = (
            self._sizes.get(target.parent.name, 0) + len(data) - old_size
        )
        self.total_bytes += len(data) - old_size

    def _bytes(self, target: Path, cap: int) -> bytes:
        if target.is_symlink():
            raise IndexError("build staging refuses symlinks")
        with target.open("rb") as handle:
            data = handle.read(cap + 1)
        if len(data) > cap:
            raise IndexError("oversized build-staging artifact")
        return data

    def _json(self, target: Path, cap: int) -> dict[str, Any]:
        try:
            value = json.loads(self._bytes(target, cap))
            if not isinstance(value, dict):
                raise ValueError("expected object")
            return value
        except (ValueError, UnicodeError) as exc:
            raise IndexError(
                "corrupt build-staging artifact; remove derived staging and rerun"
            ) from exc

    def plan(self, ref: Prepared) -> FilePlan:
        body = self._bytes(self.root / ref.key / "plan.json", self.plan_cap)
        if digest(body) != ref.plan_hash:
            raise IndexError("staged plan checksum mismatch")
        try:
            value = json.loads(body)
            value["rows"] = tuple(PendingRow(**row) for row in value["rows"])
            for key in ("embed_inputs", "chunk_ids", "stale_chunk_ids"):
                value[key] = tuple(value[key])
            value["file_row"] = PendingFileRow(**value["file_row"]) if value["file_row"] else None
            value["noop"] = IndexOutcome(**value["noop"]) if value["noop"] else None
            value["counts"] = IndexOutcome(**value["counts"])
            return FilePlan(**value)
        except (ValueError, TypeError, KeyError) as exc:
            raise IndexError("invalid staged plan") from exc

    def inputs(self, ref: Prepared, start: int, end: int) -> tuple[list[str], list[str]]:
        directory = self.root / ref.key
        index = self._bytes(directory / "inputs.idx", ref.count * _INDEX.size)
        if len(index) != ref.count * _INDEX.size or digest(index) != ref.index_hash:
            raise IndexError("staged input index checksum mismatch")
        texts, ids = [], []
        target = directory / "inputs.jsonl"
        if target.is_symlink():
            raise IndexError("build staging refuses symlinks")
        with target.open("rb") as handle:
            for position in range(start, end):
                offset, size, expected = _INDEX.unpack_from(index, position * _INDEX.size)
                if size > self.plan_cap:
                    raise IndexError("oversized staged input")
                handle.seek(offset)
                line = handle.read(size)
                if hashlib.sha256(line).digest() != expected:
                    raise IndexError("staged input checksum mismatch")
                value = json.loads(line)
                if set(value) != {"input", "chunk_id"} or not all(
                    isinstance(v, str) for v in value.values()
                ):
                    raise IndexError("invalid staged input")
                texts.append(value["input"])
                ids.append(value["chunk_id"])
        return texts, ids

    def receipt(self, ref: Prepared, start: int, end: int) -> bytes | None:
        directory = self.root / ref.key
        marker = directory / f"turn-{start}-{end}.json"
        if not marker.exists():
            return None
        value = self._json(marker, 16384)
        expected = {"key": ref.key, "start": start, "end": end, "dim": self.dim}
        if set(value) != {*expected, "sha256"} or any(
            value.get(k) != v for k, v in expected.items()
        ):
            raise IndexError("embedding receipt binding mismatch")
        data = self._bytes(directory / f"turn-{start}-{end}.f32", (end - start) * self.dim * 4)
        if len(data) != (end - start) * self.dim * 4 or digest(data) != value["sha256"]:
            raise IndexError(
                "embedding receipt checksum mismatch; inspect/remove derived index/build-staging "
                "and rerun (source files are unaffected)"
            )
        if any(not math.isfinite(v[0]) for v in struct.iter_unpack("<f", data)):
            raise IndexError("embedding receipt contains non-finite vectors")
        return data

    def save(self, ref: Prepared, start: int, end: int, vectors: Sequence[Sequence[float]]) -> None:
        if len(vectors) != end - start:
            raise IndexError("embedder returned wrong vector count")
        output = io.BytesIO()
        try:
            for vector in vectors:
                if len(vector) != self.dim or any(not math.isfinite(v) for v in vector):
                    raise IndexError("embedding turn has invalid dimension or non-finite values")
                output.write(struct.pack(f"<{self.dim}f", *vector))
        except (OverflowError, struct.error) as exc:
            raise IndexError("embedding turn is outside float32 range") from exc
        data = output.getvalue()
        directory = self.root / ref.key
        self._atomic(directory / f"turn-{start}-{end}.f32", data)
        self._atomic(
            directory / f"turn-{start}-{end}.json",
            encoded(
                {
                    "key": ref.key,
                    "start": start,
                    "end": end,
                    "dim": self.dim,
                    "sha256": digest(data),
                },
                16384,
            ),
        )

    def vector_digest(self, conn: sqlite3.Connection, plan: FilePlan) -> str | None:
        if plan.file_row is None:
            return None
        row = conn.execute(
            "SELECT file_hash, sections_json FROM files WHERE watch_root=? AND path=?",
            (plan.watch_root, plan.stored),
        ).fetchone()
        if row != (plan.file_row.file_hash, plan.file_row.sections_json):
            return None
        expected_ids = {entry[3] for entry in json.loads(plan.file_row.sections_json)}
        actual_ids = {
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM chunks WHERE watch_root=? AND path=?",
                (plan.watch_root, plan.stored),
            )
        }
        if actual_ids != expected_ids:
            return None
        result = hashlib.sha256()
        for pending in plan.rows:
            row = conn.execute(
                "SELECT body, body_hash, heading, heading_depth, frontmatter_json "
                "FROM chunks WHERE chunk_id=?",
                (pending.chunk_id,),
            ).fetchone()
            if row != (
                pending.body,
                pending.body_hash,
                pending.heading,
                pending.heading_depth,
                pending.frontmatter_json,
            ):
                return None
            keyword = conn.execute(
                "SELECT f.chunk_id, f.body FROM chunks_fts_rows AS m "
                "JOIN chunks_fts AS f ON f.rowid=m.fts_rowid WHERE m.chunk_id=?",
                (pending.chunk_id,),
            ).fetchone()
            if keyword != (pending.chunk_id, pending.body):
                return None
            vector = conn.execute(
                "SELECT embedding FROM chunks_vec WHERE chunk_id=?", (pending.chunk_id,)
            ).fetchone()
            if vector is None:
                return None
            result.update(pending.chunk_id.encode())
            result.update(bytes(vector[0]))
        return result.hexdigest()

    def published(self, ref: Prepared, plan: FilePlan, conn: sqlite3.Connection) -> bool:
        marker = self.root / ref.key / "published.json"
        if not marker.exists():
            return False
        value = self._json(marker, 16384)
        if set(value) != {"key", "vectors"} or value["key"] != ref.key:
            raise IndexError("published receipt binding mismatch")
        actual = self.vector_digest(conn, plan)
        return actual is not None and actual == value["vectors"]

    def publish(self, ref: Prepared, plan: FilePlan, conn: sqlite3.Connection) -> None:
        value = self.vector_digest(conn, plan)
        if value is None:
            raise IndexError("published document verification failed")
        directory = self.root / ref.key
        self._atomic(
            directory / "published.json", encoded({"key": ref.key, "vectors": value}, 16384)
        )
        for entry in directory.iterdir():
            if entry.name not in {"binding.json", "published.json"}:
                if entry.is_symlink() or not entry.is_file():
                    raise IndexError("unexpected build-staging entry")
                size = entry.stat().st_size
                entry.unlink()
                self._sizes[ref.key] -= size
                self.total_bytes -= size
        self.reservations.pop(ref.key, None)

    def clear_completed(self, refs: Sequence[Prepared]) -> None:
        for ref in refs:
            directory = self.root / ref.key
            if directory.exists() and (directory / "published.json").is_file():
                self._remove(directory)
        self._scan()


class ReceiptVectors(Sequence[Sequence[float]]):
    """Decode at most one turn while the existing publisher iterates vectors."""

    def __init__(self, staging: Staging, ref: Prepared, turns: Sequence[tuple[int, int]]) -> None:
        self.staging, self.ref, self.turns = staging, ref, turns

    def __len__(self) -> int:
        return self.ref.count

    @overload
    def __getitem__(self, position: int) -> Sequence[float]: ...

    @overload
    def __getitem__(self, position: slice) -> Sequence[Sequence[float]]: ...

    def __getitem__(self, position: int | slice) -> Sequence[float] | Sequence[Sequence[float]]:
        if isinstance(position, slice):
            return [self[i] for i in range(*position.indices(len(self)))]
        if position < 0:
            position += len(self)
        if not 0 <= position < len(self):
            raise builtins.IndexError(position)
        for start, end in self.turns:
            if start <= position < end:
                data = self.staging.receipt(self.ref, start, end)
                if data is None:
                    raise IndexError("missing completed embedding receipt")
                return struct.unpack_from(
                    f"<{self.staging.dim}f", data, (position - start) * self.staging.dim * 4
                )
        raise IndexError("missing embedding turn")

    def __iter__(self) -> Iterator[Sequence[float]]:
        for start, end in self.turns:
            data = self.staging.receipt(self.ref, start, end)
            if data is None:
                raise IndexError("missing completed embedding receipt")
            for position in range(end - start):
                yield struct.unpack_from(
                    f"<{self.staging.dim}f", data, position * self.staging.dim * 4
                )
