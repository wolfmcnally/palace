"""Append-only audit records for index-time cloud calls."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from palace.daemons.capture.config import BOISE_TZ
from palace.daemons.capture.ids import canonical_json_bytes, compute_record_id

__all__ = [
    "CLOUD_EGRESS_DIRNAME",
    "EgressContext",
    "EmbeddingUsage",
    "EmbeddingUsageSummary",
    "append_record",
    "build_record",
    "cloud_egress_path",
    "current_egress_context",
    "current_usage",
    "egress_batch_slice",
    "egress_context",
    "input_digest",
    "usage_scope",
]


CLOUD_EGRESS_DIRNAME = "cloud-egress"


@dataclass(frozen=True, slots=True)
class EgressContext:
    """Request provenance supplied by the indexing core."""

    watch_root: str | None
    chunk_ids: tuple[str, ...]


_EGRESS_CONTEXT: ContextVar[EgressContext | None] = ContextVar(
    "palace_index_egress_context", default=None
)
_APPEND_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class EmbeddingUsageSummary:
    """What one index invocation's embedding requests used, as the provider reported it.

    ``cost_usd`` is the sum of successful requests' reported costs, or ``None`` when any
    successful request reported none (``unknown_cost_requests``); a failed request's cost
    is never read. ``prompt_tokens`` counts successful requests' reported tokens.
    """

    requests: int = 0
    failed_requests: int = 0
    prompt_tokens: int = 0
    cost_usd: Decimal | None = Decimal(0)
    unknown_cost_requests: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failed_requests": self.failed_requests,
            "prompt_tokens": self.prompt_tokens,
            "cost_usd": None if self.cost_usd is None else format(self.cost_usd, "f"),
            "unknown_cost_requests": self.unknown_cost_requests,
        }

    def log_fields(self) -> str:
        cost = "unknown" if self.cost_usd is None else format(self.cost_usd, "f")
        return (
            f" embed_requests={self.requests} embed_failed={self.failed_requests}"
            f" prompt_tokens={self.prompt_tokens} cost_usd={cost}"
        )


class EmbeddingUsage:
    """A thread-safe running total of one invocation's embedding requests."""

    def __init__(self, parent: EmbeddingUsage | None = None) -> None:
        self._parent = (
            parent  # an enclosing total (a build's, for one root) that counts every request too
        )
        self._lock = threading.Lock()
        self._requests = 0
        self._failed = 0
        self._tokens = 0
        self._cost = Decimal(0)
        self._unknown = 0

    def record(self, *, ok: bool, prompt_tokens: int | None, cost_usd: float | None) -> None:
        """Count one request attempt; only a successful one's tokens and cost are read."""
        if self._parent is not None:
            self._parent.record(ok=ok, prompt_tokens=prompt_tokens, cost_usd=cost_usd)
        with self._lock:
            self._requests += 1
            if not ok:
                self._failed += 1
                return
            if prompt_tokens is not None:
                self._tokens += prompt_tokens
            if cost_usd is None:
                self._unknown += 1
            else:
                self._cost += Decimal(str(cost_usd))

    def summary(self) -> EmbeddingUsageSummary:
        with self._lock:
            return EmbeddingUsageSummary(
                requests=self._requests,
                failed_requests=self._failed,
                prompt_tokens=self._tokens,
                cost_usd=None if self._unknown else self._cost,
                unknown_cost_requests=self._unknown,
            )


_USAGE: ContextVar[EmbeddingUsage | None] = ContextVar("palace_index_embedding_usage", default=None)


@contextmanager
def usage_scope(usage: EmbeddingUsage | None) -> Iterator[None]:
    """Make ``usage`` the running total for embedding requests made on this thread.

    Like :func:`egress_context`, this does not cross worker-thread boundaries: a worker
    re-enters the scope with the value its invocation captured.
    """
    token = _USAGE.set(usage)
    try:
        yield
    finally:
        _USAGE.reset(token)


def current_usage() -> EmbeddingUsage | None:
    """Return the running total active on this thread, if any."""
    return _USAGE.get()


def cloud_egress_path(store: Path, day: date) -> Path:
    """Return the audit day file without touching the daemon's day-file tail."""
    return store / "events" / CLOUD_EGRESS_DIRNAME / f"{day.isoformat()}.jsonl"


@contextmanager
def egress_context(*, watch_root: str | None, chunk_ids: Sequence[str]) -> Iterator[None]:
    """Expose chunk provenance to a nested remote embed request.

    Context variables are not inherited across worker-thread boundaries. The
    synchronous builder therefore carries the :class:`EgressContext` as a
    value and enters this manager inside the worker. Callers that cannot enter
    ambient context may pass that value to :func:`build_record` explicitly.
    """
    token = _EGRESS_CONTEXT.set(EgressContext(watch_root=watch_root, chunk_ids=tuple(chunk_ids)))
    try:
        yield
    finally:
        _EGRESS_CONTEXT.reset(token)


@contextmanager
def egress_batch_slice(*, start: int, count: int, total: int) -> Iterator[None]:
    """Narrow the active provenance to one request's slice of the input list.

    A caller establishes provenance for a whole file, but an oversized input
    list is split into several requests. Each request's audit record must name
    only the chunks that request actually carried — a record listing all 1137
    of a file's chunk ids beside a 113-input digest is a false record, and an
    audit trail that overstates what left the machine is worse than none.

    Narrowing applies only when the ids line up 1:1 with the inputs; any other
    shape is left untouched rather than sliced on a guess.
    """
    context = _EGRESS_CONTEXT.get()
    if context is None or len(context.chunk_ids) != total:
        yield
        return
    token = _EGRESS_CONTEXT.set(
        EgressContext(
            watch_root=context.watch_root,
            chunk_ids=context.chunk_ids[start : start + count],
        )
    )
    try:
        yield
    finally:
        _EGRESS_CONTEXT.reset(token)


def current_egress_context() -> EgressContext | None:
    """Return provenance active on this thread, without cross-thread inheritance."""
    return _EGRESS_CONTEXT.get()


def input_digest(texts: Sequence[str]) -> str:
    """SHA-256 the exact ordered input list sent to the provider."""
    return hashlib.sha256(canonical_json_bytes(list(texts))).hexdigest()


def build_record(
    *,
    provider: str,
    upstream_configured: str | None,
    upstream_observed: str | None,
    model: str,
    dim: int,
    texts: Sequence[str],
    prompt_tokens: int | None,
    cost_usd: float | None,
    http_status: int | None,
    latency_ms: int,
    kind: str = "cloud_embed_call",
    context: EgressContext | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one canonical cloud-call record and stamp its content id last."""
    stamp = now if now is not None else datetime.now(BOISE_TZ)
    stamp_text = stamp.isoformat(timespec="seconds")
    resolved_context = context if context is not None else current_egress_context()
    record: dict[str, Any] = {
        "kind": kind,
        "event_time": stamp_text,
        "ingest_time": stamp_text,
        "provider": provider,
        "upstream_configured": upstream_configured,
        "upstream_observed": upstream_observed,
        "model": model,
        "dim": dim,
        "watch_root": resolved_context.watch_root if resolved_context is not None else None,
        "input_count": len(texts),
        "input_digest": input_digest(texts),
        "chunk_ids": list(resolved_context.chunk_ids) if resolved_context is not None else [],
        "prompt_tokens": prompt_tokens,
        "cost_usd": cost_usd,
        "http_status": http_status,
        "latency_ms": latency_ms,
    }
    record["id"] = compute_record_id(record)
    return record


def append_record(*, store: Path, record: dict[str, Any], day: date | None = None) -> Path:
    """Append one canonical LF-terminated audit record."""
    target_day = day if day is not None else datetime.now(BOISE_TZ).date()
    file_path = cloud_egress_path(store, target_day)
    with _APPEND_LOCK:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("ab") as handle:
            handle.write(canonical_json_bytes(record) + b"\n")
    return file_path
