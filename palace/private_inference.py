"""Audited, bounded HTTPS inference using the explicit palace-json-v1 adapter."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError
from palace.index.egress import append_record, build_record, current_usage, egress_batch_slice
from palace.provider_config import EndpointConfig, assert_private_boundary
from palace.rerank import RerankUnavailableError

_RESPONSE_BYTES = 16 * 1024 * 1024
_EMBED_BATCH = 128


class _Unavailable(IndexError):
    pass


class _Endpoint:
    def __init__(
        self,
        *,
        store: Path,
        config: EndpointConfig,
        model: str,
        dim: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        assert_private_boundary(store=store, watch_roots=[])
        if not isinstance(model, str) or not model.strip():
            raise IndexError("private inference requires an explicit model")
        secret, context = config.runtime()
        if any(secret in value for value in (model, config.name, config.url)):
            raise IndexError("private inference credential value appears in public configuration")
        self._store, self._config, self._model, self._dim = store, config, model, dim
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._client = httpx.Client(
            timeout=config.timeout_seconds,
            verify=context,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._closed = False

    def close(self) -> None:
        self._client.close()
        self._closed = True

    def _audit(
        self,
        *,
        operation: str,
        stage: str,
        request_id: str,
        attempt: int,
        texts: Sequence[str],
        http_status: int | None,
        latency_ms: int,
    ) -> None:
        record = build_record(
            provider=self._config.provider_id,
            upstream_configured=self._config.name,
            upstream_observed=None,
            model=self._model,
            dim=self._dim,
            texts=texts,
            prompt_tokens=None,
            cost_usd=None,
            http_status=http_status,
            latency_ms=latency_ms,
            kind=f"private_{operation}_{stage}",
        )
        record.update(request_id=request_id, attempt=attempt, operation=operation)
        record["id"] = compute_record_id(
            {key: value for key, value in record.items() if key != "id"}
        )
        try:
            append_record(store=self._store, record=record)
        except OSError:
            raise IndexError("private inference cannot append its required audit record") from None

    def _request(
        self, *, operation: str, payload: dict[str, Any], texts: Sequence[str]
    ) -> dict[str, Any]:
        if self._closed:
            raise IndexError("private inference client is closed")
        request_id = str(uuid.uuid4())
        for attempt in range(1, self._config.max_attempts + 1):
            self._audit(
                operation=operation,
                stage="attempt",
                request_id=request_id,
                attempt=attempt,
                texts=texts,
                http_status=None,
                latency_ms=0,
            )
            started = time.monotonic()
            code: int | None = None
            data = bytearray()
            transport_failed = False
            received = False
            try:
                with self._client.stream(
                    "POST", self._config.url, json=payload, headers=self._headers
                ) as response:
                    code = response.status_code
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > self._config.timeout_seconds:
                            raise httpx.ReadTimeout("bounded endpoint deadline")
                        data.extend(chunk)
                        if len(data) > _RESPONSE_BYTES:
                            raise IndexError(
                                "private inference response exceeds the adapter size limit"
                            )
                received = True
            except httpx.HTTPError:
                transport_failed = True
            finally:
                usage = current_usage() if operation == "embedding" else None
                if usage is not None:  # once per embedding attempt, before its response record
                    usage.record(
                        ok=received and code is not None and 200 <= code < 300,
                        prompt_tokens=None,
                        cost_usd=None,
                    )
                self._audit(
                    operation=operation,
                    stage="response",
                    request_id=request_id,
                    attempt=attempt,
                    texts=texts,
                    http_status=code,
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                )
            if transport_failed or code == 429 or (code is not None and 500 <= code < 600):
                if attempt < self._config.max_attempts:
                    time.sleep(0.25 * attempt)
                    continue
                raise _Unavailable(
                    "private inference endpoint is unavailable after bounded attempts"
                )
            if code in (401, 403):
                raise IndexError("private inference authentication was refused")
            if code is None or not 200 <= code < 300:
                raise IndexError(
                    "private inference endpoint refused the request; redirects are forbidden"
                )
            try:
                body = json.loads(data)
            except (ValueError, UnicodeError):
                raise IndexError("private inference response is not valid JSON") from None
            if not isinstance(body, dict):
                raise IndexError("private inference response must be an object")
            if body.get("model") != self._model or body.get("provider") != self._config.name:
                raise IndexError("private inference response model/provider identity mismatch")
            return body
        raise AssertionError("bounded endpoint loop did not terminate")


def _rows(body: dict[str, Any], count: int) -> list[dict[str, Any]]:
    values = body.get("data")
    if not isinstance(values, list) or len(values) != count:
        raise IndexError("private inference response count mismatch")
    rows: dict[int, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise IndexError("private inference response entry must be an object")
        index = value.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < count
            or index in rows
        ):
            raise IndexError("private inference response indices must be complete and unique")
        rows[index] = value
    return [rows[index] for index in range(count)]


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IndexError("private inference response contains a non-finite or nonnumeric value")
    try:
        result = float(value)
    except OverflowError:
        raise IndexError("private inference response contains an out-of-range number") from None
    if not math.isfinite(result):
        raise IndexError("private inference response contains a non-finite value")
    return result


class EndpointEmbedder(_Endpoint):
    max_concurrency = 1

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = list(texts)
        vectors = []
        for start in range(0, len(inputs), _EMBED_BATCH):
            batch = inputs[start : start + _EMBED_BATCH]
            with egress_batch_slice(start=start, count=len(batch), total=len(inputs)):
                body = self._request(
                    operation="embedding",
                    texts=batch,
                    payload={
                        "operation": "embedding",
                        "model": self._model,
                        "input": batch,
                        "dim": self._dim,
                    },
                )
            for row in _rows(body, len(batch)):
                vector = row.get("embedding")
                if not isinstance(vector, list) or len(vector) != self._dim:
                    raise IndexError("private inference embedding dimension mismatch")
                vectors.append([_finite(value) for value in vector])
        return vectors

    def probe(self) -> None:
        self.embed(["palace synthetic embedding probe"])


class EndpointReranker(_Endpoint):
    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        try:
            body = self._request(
                operation="reranking",
                texts=[query, *texts],
                payload={
                    "operation": "reranking",
                    "model": self._model,
                    "query": query,
                    "documents": list(texts),
                },
            )
        except _Unavailable:
            raise RerankUnavailableError("private reranking endpoint is unavailable") from None
        return [_finite(row.get("relevance_score")) for row in _rows(body, len(texts))]

    def probe(self) -> None:
        self.score("palace synthetic rerank probe", ["palace synthetic document"])
