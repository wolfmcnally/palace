"""Published vector indexes in Amazon S3 Vectors: opt-in, receipt, publishing and querying.

A store opted in as *published* (``policies/local-first.md`` § Published vector indexes)
may publish its chunk vectors to an S3 Vectors index in an operator-controlled AWS
account and may select that index as its vector search leg. Everything the store
decides lives under ``<store>/meta/``:

- ``vector-publishing.toml`` — the explicit per-store opt-in: ``published_corpus``,
  ``published_corpus_note`` (what is published and where) and
  ``published_corpus_asserted_at``. Written by ``palace index publishing set``.
- ``vector-backend.toml`` — the search leg: ``backend = "sqlite-vec"`` (also the meaning
  of an absent file) or ``backend = "s3vectors"`` with an optional ``profile``.
- ``vector-publication.json`` — the publication receipt: the target index, its ARN, the
  embedding identity the vectors were built with, the published key-set digest and
  counts, and ``state`` (``publishing`` while a publish is mutating the index).
- ``vector-publication-keys.txt`` — the sorted published keys; the receipt's
  ``key_set_sha256`` is the SHA-256 of exactly these bytes.

Publishing is incremental by chunk key (the content-addressed ``chunk_id``): keys the
index lacks are put first, then keys the store no longer holds are deleted, so a query
never sees a gap. A trusted receipt lets a publish diff locally; otherwise it lists the
index once. Every S3 Vectors call appends one ``cloud_vector_call`` audit record (no
chunk text, query text or credential) and counts into :class:`VectorUsage`.

boto3 is imported only when a client is opened, so ordinary palace commands and
sqlite-vec search never load it. Limits follow the S3 Vectors service model shipped
with botocore (model 2025-07-15: dimension 1..4096, 1..500 vectors per put or delete,
1..10 non-filterable metadata keys, 1..1000 keys per list page) and the AWS limits
the operator retrieved on 2026-09-28 (40 KB metadata per vector, 2 KB of it
filterable, 20 MiB request payload, top-K up to 10,000).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlite_vec
import tomlkit

from palace.daemons.capture.config import BOISE_TZ
from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError
from palace.index.config import EmbeddingIdentity, chunks_db_path
from palace.index.egress import append_record
from palace.index.embedder_config import identity_for, load_embedder_config
from palace.index.schema import assert_identity, assert_identity_readable
from palace.provider_config import assert_outside_personal_boundary, write_selector

__all__ = [
    "BACKEND_S3VECTORS",
    "BACKEND_SQLITE_VEC",
    "PublicationReceipt",
    "PublishResult",
    "PublishingConfig",
    "S3VectorsBackend",
    "VectorBackendConfig",
    "VectorMatch",
    "VectorUsage",
    "VectorUsageSummary",
    "current_vector_usage",
    "load_publishing_config",
    "load_vector_backend_config",
    "open_client",
    "publish_vectors",
    "publishing_config_path",
    "read_receipt",
    "receipt_keys_path",
    "receipt_path",
    "resolve_vector_backend",
    "save_publishing_config",
    "save_vector_backend_config",
    "vector_backend_config_path",
    "vector_usage_scope",
]

PROVIDER = "aws-s3vectors"
BACKEND_SQLITE_VEC = "sqlite-vec"
BACKEND_S3VECTORS = "s3vectors"
RECEIPT_FORMAT = "palace-vector-publication-v1"
DISTANCE_METRIC = "cosine"
DATA_TYPE = "float32"
STATE_PUBLISHING = "publishing"
STATE_COMPLETE = "complete"
# Service limits (see the module docstring for their sources).
MAX_VECTORS_PER_CALL = 500
MAX_REQUEST_BYTES = 20 * 1024 * 1024
MAX_METADATA_BYTES = 40_000
MAX_TOP_K = 10_000
LIST_PAGE_SIZE = 1000
# Transient failures of idempotent calls are retried by palace, not the SDK, so each
# attempt is audited and counted; the codes are the service model's throttling,
# unavailability, internal and timeout errors plus botocore's connection failures.
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_CODES = frozenset(
    {
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "RequestTimeoutException",
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
    }
)
# Batches are cut against an upper-bound estimate kept 2 MiB under the payload limit.
REQUEST_BUDGET_BYTES = 18 * 1024 * 1024
TEXT_METADATA_KEYS = ("text", "path", "heading")
# botocore's rest-json body is json.dumps with default separators: a float's repr is at
# most 24 characters, plus ", " — so 26 bytes bounds each vector element.
_FLOAT_JSON_BOUND = 26
_VECTOR_ENVELOPE_BYTES = 256
_sleep = time.sleep  # the retry backoff; a test replaces this seam, not the time module
_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PUBLISHING_KEYS = frozenset(
    {"published_corpus", "published_corpus_note", "published_corpus_asserted_at"}
)


# ---------------------------------------------------------------- selectors


def publishing_config_path(store: Path) -> Path:
    return store / "meta" / "vector-publishing.toml"


def vector_backend_config_path(store: Path) -> Path:
    return store / "meta" / "vector-backend.toml"


def receipt_path(store: Path) -> Path:
    return store / "meta" / "vector-publication.json"


def receipt_keys_path(store: Path) -> Path:
    return store / "meta" / "vector-publication-keys.txt"


@dataclass(frozen=True, slots=True)
class PublishingConfig:
    """The store's explicit published-corpus assertion for vector publication."""

    published_corpus_note: str
    published_corpus_asserted_at: str


def _parse_toml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — normalize parser and I/O failures
        raise IndexError(f"{label} {path} is not readable TOML") from None


def _zoned(stamp: object, label: str) -> str:
    if not isinstance(stamp, str):
        raise IndexError(f"{label} must be a timestamp with a zone")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise IndexError(f"{label} must be a timestamp with a zone") from None
    if parsed.tzinfo is None:
        raise IndexError(f"{label} must be a timestamp with a zone")
    return stamp


def load_publishing_config(store: Path) -> PublishingConfig | None:
    """Read the opt-in; ``None`` when the store has not opted in."""
    path = publishing_config_path(store)
    if not path.exists():
        return None
    document = _parse_toml(path, "vector publishing opt-in")
    if set(document) != _PUBLISHING_KEYS:
        raise IndexError(
            "vector publishing opt-in requires exactly published_corpus, "
            "published_corpus_note and published_corpus_asserted_at — rewrite it with "
            "'palace index publishing set --published-corpus --published-corpus-note <text>'"
        )
    if document["published_corpus"] is not True:
        raise IndexError("vector publishing opt-in must assert published_corpus = true")
    note = document["published_corpus_note"]
    if not isinstance(note, str) or not note.strip():
        raise IndexError("vector publishing opt-in requires a non-empty published_corpus_note")
    stamp = _zoned(document["published_corpus_asserted_at"], "published_corpus_asserted_at")
    return PublishingConfig(published_corpus_note=str(note), published_corpus_asserted_at=stamp)


def save_publishing_config(
    store: Path, *, note: str, now: datetime | None = None
) -> PublishingConfig:
    """Record the opt-in, stamped now; the personal store is refused outright."""
    if not isinstance(note, str) or not note.strip():
        raise IndexError("vector publishing opt-in requires a non-empty published-corpus note")
    assert_outside_personal_boundary(store=store, watch_roots=(), operation="vector publishing")
    stamp = (now if now is not None else datetime.now(BOISE_TZ)).isoformat(timespec="seconds")
    write_selector(
        publishing_config_path(store),
        {
            "published_corpus": True,
            "published_corpus_note": note,
            "published_corpus_asserted_at": stamp,
        },
    )
    return PublishingConfig(published_corpus_note=note, published_corpus_asserted_at=stamp)


@dataclass(frozen=True, slots=True)
class VectorBackendConfig:
    """The store's vector search leg; sqlite-vec unless explicitly selected."""

    backend: str = BACKEND_SQLITE_VEC
    profile: str | None = None


def load_vector_backend_config(store: Path) -> VectorBackendConfig:
    path = vector_backend_config_path(store)
    if not path.exists():
        return VectorBackendConfig()
    document = _parse_toml(path, "vector backend selector")
    backend = document.get("backend")
    if backend == BACKEND_SQLITE_VEC and set(document) == {"backend"}:
        return VectorBackendConfig()
    if backend != BACKEND_S3VECTORS or not set(document) <= {"backend", "profile"}:
        raise IndexError(
            "vector backend selector requires backend = 'sqlite-vec', or backend = "
            "'s3vectors' with an optional profile"
        )
    profile = document.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise IndexError("vector backend profile must be a non-empty string")
    return VectorBackendConfig(BACKEND_S3VECTORS, None if profile is None else str(profile))


def save_vector_backend_config(store: Path, config: VectorBackendConfig) -> None:
    if config.backend not in (BACKEND_SQLITE_VEC, BACKEND_S3VECTORS):
        raise IndexError(f"unknown vector backend {config.backend!r}")
    document: dict[str, object] = {"backend": config.backend}
    if config.profile is not None:
        if config.backend != BACKEND_S3VECTORS or not config.profile.strip():
            raise IndexError(
                "a vector backend profile applies only to a non-empty s3vectors profile"
            )
        document["profile"] = config.profile
    write_selector(vector_backend_config_path(store), document)


# ------------------------------------------------------------------ receipt


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """What the store last published, and where."""

    state: str
    region: str
    bucket: str
    index: str
    index_arn: str
    dimension: int
    text_metadata: bool
    embedder: EmbeddingIdentity
    published_at: str
    key_set_sha256: str | None = None
    vector_count: int | None = None
    put: int | None = None
    deleted: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": RECEIPT_FORMAT,
            "backend": BACKEND_S3VECTORS,
            "state": self.state,
            "region": self.region,
            "bucket": self.bucket,
            "index": self.index,
            "index_arn": self.index_arn,
            "dimension": self.dimension,
            "distance_metric": DISTANCE_METRIC,
            "text_metadata": self.text_metadata,
            "embedder": {
                "convention": self.embedder.convention,
                "model": self.embedder.model,
                "provider": self.embedder.provider,
                "dim": self.embedder.dim,
            },
            "key_set_sha256": self.key_set_sha256,
            "vector_count": self.vector_count,
            "put": self.put,
            "deleted": self.deleted,
            "published_at": self.published_at,
        }


def _receipt_from(value: object) -> PublicationReceipt:
    if not isinstance(value, dict):
        raise ValueError("not an object")
    if value.get("format") != RECEIPT_FORMAT or value.get("backend") != BACKEND_S3VECTORS:
        raise ValueError("unknown format")
    if value.get("state") not in (STATE_PUBLISHING, STATE_COMPLETE):
        raise ValueError("unknown state")
    if value.get("distance_metric") != DISTANCE_METRIC:
        raise ValueError("unknown distance")
    embedder = value.get("embedder")
    if not isinstance(embedder, dict):
        raise ValueError("no embedder")
    identity = EmbeddingIdentity(
        convention=_text(embedder.get("convention")),
        model=_text(embedder.get("model")),
        provider=_text(embedder.get("provider")),
        dim=_count(embedder.get("dim")),
    )
    complete = value["state"] == STATE_COMPLETE
    digest = value.get("key_set_sha256")
    if complete and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("no key-set digest")
    text_metadata = value.get("text_metadata")
    if not isinstance(text_metadata, bool):
        raise ValueError("no text_metadata flag")
    return PublicationReceipt(
        state=str(value["state"]),
        region=_text(value.get("region")),
        bucket=_text(value.get("bucket")),
        index=_text(value.get("index")),
        index_arn=_text(value.get("index_arn")),
        dimension=_count(value.get("dimension")),
        text_metadata=text_metadata,
        embedder=identity,
        published_at=_text(value.get("published_at")),
        key_set_sha256=digest if complete else None,
        vector_count=_count(value.get("vector_count")) if complete else None,
        put=_count(value.get("put")) if complete else None,
        deleted=_count(value.get("deleted")) if complete else None,
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty string")
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def read_receipt(store: Path) -> PublicationReceipt | None:
    """Read the publication receipt; ``None`` when the store has never published."""
    path = receipt_path(store)
    if not path.exists():
        return None
    try:
        return _receipt_from(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise IndexError(f"vector publication receipt {path} is unreadable: {exc}") from None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_receipt(store: Path, receipt: PublicationReceipt) -> None:
    body = json.dumps(receipt.as_dict(), sort_keys=True, indent=2) + "\n"
    _atomic_write(receipt_path(store), body.encode("utf-8"))


def _key_set_bytes(keys: Sequence[str]) -> bytes:
    return "".join(f"{key}\n" for key in sorted(keys)).encode("ascii")


def _trusted_keys(store: Path, receipt: PublicationReceipt) -> frozenset[str] | None:
    """The sidecar's keys when their digest matches the receipt's, else ``None``."""
    try:
        data = receipt_keys_path(store).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != receipt.key_set_sha256:
        return None
    return frozenset(data.decode("ascii").splitlines())


# -------------------------------------------------------------------- usage


@dataclass(frozen=True, slots=True)
class VectorUsageSummary:
    """What one invocation's S3 Vectors calls cost, as palace measured them.

    Every request attempt counts, retries included (the SDK makes none of its own).
    ``request_bytes`` sums each attempt's request body as palace hands it to the SDK
    (the JSON botocore sends). ``failed_requests`` counts attempts that failed, except
    an expected not-found probe.
    """

    requests: int = 0
    failed_requests: int = 0
    request_bytes: int = 0
    operations: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failed_requests": self.failed_requests,
            "request_bytes": self.request_bytes,
            "operations": dict(sorted(self.operations.items())),
        }

    def log_fields(self) -> str:
        return (
            f" requests={self.requests} failed={self.failed_requests}"
            f" request_bytes={self.request_bytes}"
        )


class VectorUsage:
    """A thread-safe running total of S3 Vectors calls; an enclosing total counts too."""

    def __init__(self, parent: VectorUsage | None = None) -> None:
        self._parent = parent
        self._lock = threading.Lock()
        self._requests = 0
        self._failed = 0
        self._bytes = 0
        self._operations: dict[str, int] = {}

    def record(self, *, operation: str, ok: bool, request_bytes: int) -> None:
        if self._parent is not None:
            self._parent.record(operation=operation, ok=ok, request_bytes=request_bytes)
        with self._lock:
            self._requests += 1
            self._bytes += request_bytes
            self._operations[operation] = self._operations.get(operation, 0) + 1
            if not ok:
                self._failed += 1

    def summary(self) -> VectorUsageSummary:
        with self._lock:
            return VectorUsageSummary(
                requests=self._requests,
                failed_requests=self._failed,
                request_bytes=self._bytes,
                operations=dict(self._operations),
            )


_VECTOR_USAGE: ContextVar[VectorUsage | None] = ContextVar("palace_vector_usage", default=None)


@contextmanager
def vector_usage_scope(usage: VectorUsage | None) -> Iterator[None]:
    """Make ``usage`` the enclosing total for S3 Vectors clients opened on this thread."""
    token = _VECTOR_USAGE.set(usage)
    try:
        yield
    finally:
        _VECTOR_USAGE.reset(token)


def current_vector_usage() -> VectorUsage | None:
    return _VECTOR_USAGE.get()


# ------------------------------------------------------------------- client


def open_client(profile: str | None, region: str) -> Any:
    """Construct the boto3 ``s3vectors`` client; the only place boto3 is imported.

    SDK retries are disabled (one total attempt; botocore's ``max_attempts`` would count
    retries after the first) so every request attempt passes through
    :class:`_AuditedClient`, which audits, counts and retries it itself.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        return session.client(
            "s3vectors", config=Config(retries={"mode": "standard", "total_max_attempts": 1})
        )
    except BotoCoreError as exc:
        raise IndexError(f"S3 Vectors client could not be opened: {exc}") from None


def _error_code(exc: BaseException) -> str | None:
    """The AWS error code of a botocore failure; ``None`` for anything else."""
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return None


class _AuditedClient:
    """Every S3 Vectors request palace makes: audited, counted, retried, errors made plain."""

    def __init__(self, client: Any, *, store: Path, region: str, bucket: str, index: str) -> None:
        self._client = client
        self._store = store
        self._region = region
        self._bucket = bucket
        self._index = index
        self.index_arn: str | None = None
        self.usage = VectorUsage(parent=current_vector_usage())

    def call(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        keys: Sequence[str] = (),
        input_count: int = 0,
        not_found: bool = False,
    ) -> dict[str, Any] | None:
        """Make one logical call; each attempt is its own audited, counted request.

        Idempotent operations retry transient failures up to :data:`MAX_ATTEMPTS`
        times; creates never retry (a rerun converges). ``not_found`` turns an expected
        ``NotFoundException`` into ``None``.
        """
        body = json.dumps(dict(params)).encode("utf-8")
        method = getattr(self._client, _snake(operation))
        attempts = 1 if operation.startswith("Create") else MAX_ATTEMPTS
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = method(**params)
            except Exception as exc:  # noqa: BLE001 — only botocore failures are translated
                code = _error_code(exc)
                if code is None:
                    raise
                expected = not_found and code == "NotFoundException"
                self._audit(operation, body, keys, input_count, code, attempt, started, ok=expected)
                if expected:
                    return None
                if code in RETRYABLE_CODES and attempt < attempts:
                    _sleep(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)))
                    continue
                raise IndexError(
                    f"S3 Vectors {operation} failed: {code}: {_message(exc)}"
                ) from None
            self._audit(operation, body, keys, input_count, "ok", attempt, started, ok=True)
            return dict(response) if isinstance(response, Mapping) else {}
        raise AssertionError("unreachable: the final attempt returns or raises")

    def _audit(
        self,
        operation: str,
        body: bytes,
        keys: Sequence[str],
        input_count: int,
        status: str,
        attempt: int,
        started: float,
        *,
        ok: bool,
    ) -> None:
        self.usage.record(operation=operation, ok=ok, request_bytes=len(body))
        stamp = datetime.now(BOISE_TZ).isoformat(timespec="seconds")
        record: dict[str, Any] = {
            "kind": "cloud_vector_call",
            "event_time": stamp,
            "ingest_time": stamp,
            "provider": PROVIDER,
            "operation": operation,
            "region": self._region,
            "bucket": self._bucket,
            "index": self._index,
            "index_arn": self.index_arn,
            "input_count": input_count,
            "chunk_ids": list(keys),
            "input_digest": hashlib.sha256(body).hexdigest(),
            "request_bytes": len(body),
            "status": status,
            "attempt": attempt,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        record["id"] = compute_record_id(record)
        try:
            append_record(store=self._store, record=record)
        except OSError:
            raise IndexError("S3 Vectors call cannot append its required audit record") from None


def _snake(operation: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation).lower()


def _message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping) and error.get("Message"):
            return str(error["Message"])
    return str(exc)


# ------------------------------------------------------------------ boundary


def _assert_publishable(store: Path, watch_roots: Sequence[Path]) -> PublishingConfig:
    """The personal-store and vault refusals, then a complete opt-in."""
    assert_outside_personal_boundary(
        store=store, watch_roots=watch_roots, operation="vector publishing"
    )
    config = load_publishing_config(store)
    if config is None:
        raise IndexError(
            "store is not opted in as published — run 'palace index publishing set "
            "--published-corpus --published-corpus-note <what is published and where>'"
        )
    return config


def _open_snapshot(store: Path) -> sqlite3.Connection:
    """A read-only connection holding one read transaction over the chunks DB."""
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        raise IndexError(f"store has no chunks database: {store}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("BEGIN")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _watch_roots(conn: sqlite3.Connection) -> list[Path]:
    return [Path(row[0]) for row in conn.execute("SELECT DISTINCT watch_root FROM chunks")]


def _checked_identity(conn: sqlite3.Connection, store: Path) -> EmbeddingIdentity:
    identity = identity_for(load_embedder_config(store))
    assert_identity_readable(conn)
    assert_identity(conn, identity)
    return identity


def _divergence(found: EmbeddingIdentity, expected: EmbeddingIdentity) -> str:
    return "; ".join(
        f"{axis} published='{getattr(found, axis)}' store='{getattr(expected, axis)}'"
        for axis in ("convention", "model", "provider", "dim")
        if getattr(found, axis) != getattr(expected, axis)
    )


# ---------------------------------------------------------------- publishing


@dataclass(frozen=True, slots=True)
class PublishResult:
    """One publish: what changed remotely and what it cost."""

    index_arn: str
    created_bucket: bool
    created_index: bool
    listed_remote: bool
    put: int
    deleted: int
    unchanged: int
    vector_count: int
    text_metadata: bool
    usage: VectorUsageSummary

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_arn": self.index_arn,
            "created_bucket": self.created_bucket,
            "created_index": self.created_index,
            "listed_remote": self.listed_remote,
            "put": self.put,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "vector_count": self.vector_count,
            "text_metadata": self.text_metadata,
            "usage": self.usage.as_dict(),
        }


def _validate_target(bucket: str, index: str, region: str, profile: str | None) -> None:
    for label, name in (("bucket", bucket), ("index", index)):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise IndexError(
                f"S3 Vectors {label} name must be 3-63 lowercase letters, digits, '.' or '-', "
                f"starting and ending with a letter or digit: {name!r}"
            )
    if not isinstance(region, str) or not region.strip():
        raise IndexError("S3 Vectors publishing requires a region")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise IndexError("S3 Vectors profile must be a non-empty string")


def _check_text_metadata(conn: sqlite3.Connection) -> None:
    """Refuse before any call when a vector has no chunk row or too much text.

    The measured size is the UTF-8 bytes of the three keys and their values, the form
    the service limits; it is computed in SQL so no chunk text is loaded here.
    """
    missing = conn.execute(
        "SELECT count(*), min(chunk_id) FROM chunks_vec "
        "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
    ).fetchone()
    if missing[0]:
        raise IndexError(
            f"{missing[0]} vectors have no chunk row for text metadata (first {missing[1]})"
        )
    key_bytes = sum(len(key) for key in TEXT_METADATA_KEYS)
    oversize = conn.execute(
        "SELECT count(*), min(chunk_id) FROM chunks WHERE chunk_id IN "
        "(SELECT chunk_id FROM chunks_vec) AND ? + length(CAST(body AS BLOB)) "
        "+ length(CAST(path AS BLOB)) + length(CAST(coalesce(heading, '') AS BLOB)) > ?",
        (key_bytes, MAX_METADATA_BYTES),
    ).fetchone()
    if oversize[0]:
        raise IndexError(
            f"{oversize[0]} chunks exceed the {MAX_METADATA_BYTES}-byte text-metadata limit "
            f"(first {oversize[1]}); publish without --text-metadata"
        )


def _chunk_metadata(conn: sqlite3.Connection, key: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT body, path, heading FROM chunks WHERE chunk_id = ?", (key,)
    ).fetchone()
    if row is None:
        raise IndexError(f"chunk row vanished from the read snapshot: {key}")
    body, path, heading = row
    return {"text": str(body), "path": str(path), "heading": str(heading or "")}


def _vector(conn: sqlite3.Connection, key: str, dim: int) -> list[float]:
    row = conn.execute("SELECT embedding FROM chunks_vec WHERE chunk_id = ?", (key,)).fetchone()
    if row is None:
        raise IndexError(f"chunk vector vanished from the read snapshot: {key}")
    blob = bytes(row[0])
    if len(blob) != 4 * dim:
        raise IndexError(f"chunk vector {key} has {len(blob) // 4} dimensions, expected {dim}")
    return list(struct.unpack(f"<{dim}f", blob))


def _list_remote(client: _AuditedClient, index_arn: str) -> set[str]:
    keys: set[str] = set()
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "indexArn": index_arn,
            "maxResults": LIST_PAGE_SIZE,
            "returnData": False,
            "returnMetadata": False,
        }
        if token is not None:
            params["nextToken"] = token
        response = client.call("ListVectors", params)
        assert response is not None
        keys.update(str(item["key"]) for item in response.get("vectors", []))
        next_token = response.get("nextToken")
        if not next_token:
            return keys
        token = str(next_token)


def _ensure_index(
    client: _AuditedClient, *, bucket: str, index: str, dim: int, text_metadata: bool
) -> tuple[str, bool, bool]:
    """Create or validate the target; return its ARN and what was created."""
    created_bucket = False
    if client.call("GetVectorBucket", {"vectorBucketName": bucket}, not_found=True) is None:
        client.call("CreateVectorBucket", {"vectorBucketName": bucket})
        created_bucket = True
    found = client.call(
        "GetIndex", {"vectorBucketName": bucket, "indexName": index}, not_found=True
    )
    if found is None:
        created = client.call(
            "CreateIndex",
            {
                "vectorBucketName": bucket,
                "indexName": index,
                "dataType": DATA_TYPE,
                "dimension": dim,
                "distanceMetric": DISTANCE_METRIC,
                "metadataConfiguration": {"nonFilterableMetadataKeys": list(TEXT_METADATA_KEYS)},
            },
        )
        assert created is not None
        return str(created["indexArn"]), created_bucket, True
    described = found.get("index", {})
    problems = []
    if described.get("dataType") != DATA_TYPE:
        problems.append(f"data type {described.get('dataType')!r} (expected {DATA_TYPE!r})")
    if described.get("dimension") != dim:
        problems.append(f"dimension {described.get('dimension')!r} (store records {dim})")
    if described.get("distanceMetric") != DISTANCE_METRIC:
        problems.append(
            f"distance {described.get('distanceMetric')!r} (expected {DISTANCE_METRIC!r})"
        )
    if problems:
        raise IndexError(
            f"existing S3 Vectors index {index} is incompatible: " + "; ".join(problems)
        )
    non_filterable = set(
        (described.get("metadataConfiguration") or {}).get("nonFilterableMetadataKeys") or ()
    )
    if text_metadata and not set(TEXT_METADATA_KEYS) <= non_filterable:
        raise IndexError(
            f"existing S3 Vectors index {index} does not declare text, path and heading as "
            "non-filterable metadata, so text metadata cannot be published to it"
        )
    return str(described["indexArn"]), created_bucket, False


def publish_vectors(
    *,
    store: Path,
    bucket: str,
    index: str,
    region: str,
    profile: str | None,
    text_metadata: bool = False,
) -> PublishResult:
    """Publish the store's chunk vectors to an S3 Vectors index, incrementally by key.

    Puts precede deletes. The usage spent before a failure is attached to the raised
    :class:`IndexError` as ``vector_usage``.
    """
    _validate_target(bucket, index, region, profile)
    client: _AuditedClient | None = None
    try:
        with closing(_open_snapshot(store)) as conn:
            identity = _checked_identity(conn, store)
            _assert_publishable(store, _watch_roots(conn))
            store_keys = {str(row[0]) for row in conn.execute("SELECT chunk_id FROM chunks_vec")}
            if text_metadata:
                _check_text_metadata(conn)
            try:
                receipt = read_receipt(store)
            except IndexError:
                receipt = None
            client = _AuditedClient(
                open_client(profile, region), store=store, region=region, bucket=bucket, index=index
            )
            index_arn, created_bucket, created_index = _ensure_index(
                client, bucket=bucket, index=index, dim=identity.dim, text_metadata=text_metadata
            )
            client.index_arn = index_arn
            # A complete receipt for this index and identity vouches for what the remote
            # vectors are; its sidecar, when intact, also says which keys exist. Chunk ids
            # encode neither the identity nor the metadata mode, so without a vouching
            # receipt (none, unreadable, another index or identity, or an interrupted
            # publish) every store key is put again.
            vouched = (
                receipt is not None
                and receipt.state == STATE_COMPLETE
                and receipt.index_arn == index_arn
                and receipt.embedder == identity
            )
            sidecar = _trusted_keys(store, receipt) if vouched and receipt is not None else None
            listed = False
            if created_index:
                prior: set[str] = set()
                republish_all = False
            else:
                if sidecar is not None:
                    prior = set(sidecar)
                else:
                    prior = _list_remote(client, index_arn)
                    listed = True
                republish_all = (
                    not vouched or receipt is None or receipt.text_metadata != text_metadata
                )
            to_put = sorted(store_keys if republish_all else store_keys - prior)
            to_delete = sorted(prior - store_keys)
            stamp = datetime.now(BOISE_TZ).isoformat(timespec="seconds")
            publishing = PublicationReceipt(
                state=STATE_PUBLISHING,
                region=region,
                bucket=bucket,
                index=index,
                index_arn=index_arn,
                dimension=identity.dim,
                text_metadata=text_metadata,
                embedder=identity,
                published_at=stamp,
            )
            if to_put or to_delete:
                _write_receipt(store, publishing)
                _put(client, conn, index_arn, to_put, identity.dim, text_metadata)
                for start in range(0, len(to_delete), MAX_VECTORS_PER_CALL):
                    batch = to_delete[start : start + MAX_VECTORS_PER_CALL]
                    client.call(
                        "DeleteVectors",
                        {"indexArn": index_arn, "keys": batch},
                        keys=batch,
                        input_count=len(batch),
                    )
            intact = (
                sidecar is not None
                and receipt is not None
                and receipt.text_metadata == text_metadata
                and not to_put
                and not to_delete
            )
            if not intact:
                key_bytes = _key_set_bytes(sorted(store_keys))
                _atomic_write(receipt_keys_path(store), key_bytes)
                _write_receipt(
                    store,
                    replace(
                        publishing,
                        state=STATE_COMPLETE,
                        key_set_sha256=hashlib.sha256(key_bytes).hexdigest(),
                        vector_count=len(store_keys),
                        put=len(to_put),
                        deleted=len(to_delete),
                    ),
                )
            return PublishResult(
                index_arn=index_arn,
                created_bucket=created_bucket,
                created_index=created_index,
                listed_remote=listed,
                put=len(to_put),
                deleted=len(to_delete),
                unchanged=len(store_keys) - len(to_put),
                vector_count=len(store_keys),
                text_metadata=text_metadata,
                usage=client.usage.summary(),
            )
    except (sqlite3.Error, OSError) as exc:
        # A store read or a receipt, key-list or opt-in write failed; requests already made
        # stay counted, and an interrupted receipt makes the next publish converge.
        error = IndexError(f"vector publishing failed reading or recording store state: {exc}")
        error.vector_usage = client.usage.summary() if client else VectorUsageSummary()  # type: ignore[attr-defined]
        raise error from exc
    except IndexError as exc:
        exc.vector_usage = client.usage.summary() if client else VectorUsageSummary()  # type: ignore[attr-defined]
        raise


def _put(
    client: _AuditedClient,
    conn: sqlite3.Connection,
    index_arn: str,
    keys: Sequence[str],
    dim: int,
    text_metadata: bool,
) -> None:
    """Put ``keys`` in order, cut into calls within the vector and payload limits."""
    batch: list[dict[str, Any]] = []
    estimate = 0
    for key in keys:
        item: dict[str, Any] = {"key": key, "data": {"float32": _vector(conn, key, dim)}}
        if text_metadata:
            item["metadata"] = _chunk_metadata(conn, key)
        size = (
            _FLOAT_JSON_BOUND * dim
            + len(json.dumps({"key": key, "metadata": item.get("metadata")}))
            + _VECTOR_ENVELOPE_BYTES
        )
        if batch and (len(batch) >= MAX_VECTORS_PER_CALL or estimate + size > REQUEST_BUDGET_BYTES):
            _put_batch(client, index_arn, batch)
            batch, estimate = [], 0
        batch.append(item)
        estimate += size
    if batch:
        _put_batch(client, index_arn, batch)


def _put_batch(client: _AuditedClient, index_arn: str, batch: list[dict[str, Any]]) -> None:
    keys = [str(item["key"]) for item in batch]
    client.call(
        "PutVectors", {"indexArn": index_arn, "vectors": batch}, keys=keys, input_count=len(batch)
    )


# ------------------------------------------------------------------- query


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One vector-leg result; metadata is present only when the index carries it."""

    chunk_id: str
    distance: float
    text: str | None = None
    path: str | None = None
    heading: str | None = None


class S3VectorsBackend:
    """The vector leg for a store that selected its published S3 Vectors index."""

    def __init__(self, *, store: Path, receipt: PublicationReceipt, profile: str | None) -> None:
        self._receipt = receipt
        self._client = _AuditedClient(
            open_client(profile, receipt.region),
            store=store,
            region=receipt.region,
            bucket=receipt.bucket,
            index=receipt.index,
        )
        self._client.index_arn = receipt.index_arn

    @property
    def usage(self) -> VectorUsageSummary:
        return self._client.usage.summary()

    def query(self, vector: Sequence[float], top_k: int) -> list[VectorMatch]:
        if top_k <= 0 or top_k > MAX_TOP_K:
            raise IndexError(f"S3 Vectors top-K must be 1..{MAX_TOP_K}, not {top_k}")
        if len(vector) != self._receipt.dimension:
            raise IndexError(
                f"query vector has {len(vector)} dimensions; the index holds "
                f"{self._receipt.dimension}"
            )
        params: dict[str, Any] = {
            "indexArn": self._receipt.index_arn,
            "topK": top_k,
            "queryVector": {"float32": [float(value) for value in vector]},
            "returnDistance": True,
            "returnMetadata": self._receipt.text_metadata,
        }
        matches: list[VectorMatch] = []
        # The service returns results in pages; the continuation repeats the same
        # parameters with the page's token until the token is absent.
        while True:
            response = self._client.call("QueryVectors", params, input_count=1)
            assert response is not None
            for item in response.get("vectors", []):
                carried = item.get("metadata") if self._receipt.text_metadata else None
                carried = carried if isinstance(carried, Mapping) else {}
                matches.append(
                    VectorMatch(
                        chunk_id=str(item["key"]),
                        distance=float(item.get("distance", 0.0)),
                        text=_optional_text(carried.get("text")),
                        path=_optional_text(carried.get("path")),
                        heading=_optional_text(carried.get("heading")) or None,
                    )
                )
            token = response.get("nextToken")
            if not token or len(matches) >= top_k:
                return matches[:top_k]
            params = {**params, "nextToken": str(token)}


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def resolve_vector_backend(
    *, store: Path, identity: EmbeddingIdentity, watch_roots: Sequence[Path]
) -> S3VectorsBackend | None:
    """The store's selected vector leg: ``None`` for sqlite-vec, else a checked backend.

    Refuses before any network call when the store is outside the publishing boundary,
    has no complete receipt, or its receipt records another embedding identity.
    """
    selected = load_vector_backend_config(store)
    if selected.backend == BACKEND_SQLITE_VEC:
        return None
    _assert_publishable(store, watch_roots)
    receipt = read_receipt(store)
    if receipt is None:
        raise IndexError(
            "s3vectors backend selected but the store has no publication receipt — "
            "run 'palace index publish-vectors' first"
        )
    if receipt.state != STATE_COMPLETE:
        raise IndexError("s3vectors backend selected but the last publish did not complete")
    if receipt.embedder != identity:
        raise IndexError(
            "vector publication identity mismatch: " + _divergence(receipt.embedder, identity)
        )
    return S3VectorsBackend(store=store, receipt=receipt, profile=selected.profile)
