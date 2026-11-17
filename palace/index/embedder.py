"""Embedder protocol with local Ollama and audited OpenRouter implementations.

Palace ships two implementations:

- :class:`OllamaEmbedder` — the production implementation. POSTs to
  ``{base_url}/api/embed`` with the JSON shape Ollama documents:
  ``{"model": EMBEDDER_MODEL, "input": [str, ...]}``. The startup probe
  raises a single-line :class:`palace.index._errors.IndexError` whose
  message names the operator fix (start Ollama; pull the model).
- ``FakeEmbedder`` — defined under ``tests/index/conftest.py``, not here.

The HTTP client is :class:`httpx.Client` (sync). Ollama keeps one long-lived
client and a measured concurrency capability of one. OpenRouter lazily keeps
one client per calling thread, each constructed through the suite's
hermeticity seam, and closes the complete registry. The OpenRouter startup
probe is single-attempt even though ordinary remote batches have bounded
retry. Every OpenRouter request goes through :func:`palace.index.pacing.perform`:
an optional shared :class:`~palace.index.pacing.RequestPacer` admits each physical
attempt, a :class:`~palace.index.pacing.RetryPolicy` spaces retries with jitter under
any ``Retry-After`` floor, an active :func:`~palace.index.pacing.call_scope` bounds the
waits and each attempt's HTTP timeout, and a failed request ends in a typed
:class:`palace.index.ProviderCallError`. Tests use ``httpx.MockTransport``; only the
smoke needs live Ollama.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from palace.index._errors import Delivery, FailureKind, IndexError
from palace.index.config import (
    EMBED_DIM,
    EMBEDDER_MODEL,
    OLLAMA_BASE_URL,
    OPENROUTER_BASE_URL,
    REMOTE_EMBED_MAX_BATCH,
    REMOTE_EMBED_MAX_CONCURRENCY,
    REMOTE_EMBED_MAX_TOKEN_BUDGET,
    REMOTE_EMBED_RETRY_STATUS_MIN,
)
from palace.index.egress import append_record, build_record, current_usage, egress_batch_slice
from palace.index.pacing import (
    AttemptFailure,
    AttemptInfo,
    PacedRequest,
    Purpose,
    RequestPacer,
    RetryPolicy,
    parse_retry_after,
    perform,
)

__all__ = ["Embedder", "OllamaEmbedder", "OpenRouterEmbedder"]


# A short literal palace uses for the startup probe. Distinctive enough
# that a stray hit in the chunks table would be obviously synthetic.
_PROBE_INPUT: str = "__palace_probe__"


def _build_client(*, timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    """Construct an HTTP client through the suite's hermeticity seam."""
    return httpx.Client(timeout=timeout, transport=transport)


def request_units(text: str) -> int:
    """Conservative normalized/raw request sizing in UTF-8 bytes, shared with the build
    scheduler: the larger of the raw and NFC byte lengths plus eight bytes of framing
    allowance. Not a token count."""
    return (
        max(len(text.encode("utf-8")), len(unicodedata.normalize("NFC", text).encode("utf-8"))) + 8
    )


class Embedder(Protocol):
    """The minimal embedder contract palace.index depends on.

    Implementations are safe to call from up to ``max_concurrency`` threads
    concurrently. Implementations returning 1 retain the single-caller contract.
    """

    @property
    def max_concurrency(self) -> int:
        """Maximum safe number of concurrent :meth:`embed` callers."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one ``EMBED_DIM``-element vector per input text, in order."""
        ...

    def probe(self) -> None:
        """Verify reachability + model availability at daemon startup.

        Implementations raise :class:`IndexError` with a single-line
        message naming the operator fix when the embedder is not ready.
        """
        ...

    def close(self) -> None:
        """Release any held resources (e.g., the HTTP client)."""
        ...


class OllamaEmbedder:
    """The production embedder, backed by a local Ollama daemon.

    Single long-lived :class:`httpx.Client` per instance. The writer
    thread is the only caller; no concurrent ``embed`` calls happen.
    """

    # briefs/embedding-provider-and-index-identity.md §2.1 measured four
    # local workers slower than one; this is a measured capability limit.
    max_concurrency: int = 1

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = EMBEDDER_MODEL,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = _build_client(timeout=timeout, transport=transport)

    # ---------------------------------------------------------------- API

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """POST ``texts`` as one batch to ``{base}/api/embed``."""
        if not texts:
            return []
        payload = {"model": self._model, "input": list(texts)}
        try:
            response = self._client.post(f"{self._base_url}/api/embed", json=payload)
        except httpx.RequestError as exc:
            raise IndexError(f"ollama request failed: {exc}") from exc
        if response.status_code >= 500:
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")
        if response.status_code >= 400:
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise IndexError(f"ollama response is not valid JSON: {exc}") from exc
        vectors = body.get("embeddings")
        if not isinstance(vectors, list):
            raise IndexError("ollama response missing 'embeddings' array")
        if len(vectors) != len(texts):
            raise IndexError(f"ollama returned {len(vectors)} vectors for {len(texts)} inputs")
        result: list[list[float]] = []
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list):
                raise IndexError(f"ollama vector #{index} is not a list")
            if len(vector) != EMBED_DIM:
                raise IndexError(
                    f"ollama vector #{index} has dim {len(vector)}; expected {EMBED_DIM}"
                )
            result.append([float(v) for v in vector])
        return result

    def probe(self) -> None:
        """Verify the daemon is reachable and the model is pulled.

        Translates the three operator-relevant failure shapes into
        :class:`IndexError` with single-line messages naming the fix.
        """
        payload = {"model": self._model, "input": [_PROBE_INPUT]}
        try:
            response = self._client.post(f"{self._base_url}/api/embed", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise IndexError(
                f"ollama unreachable at {self._base_url} — is the daemon running?"
            ) from exc
        except httpx.RequestError as exc:
            raise IndexError(f"ollama probe failed: {exc}") from exc

        if response.status_code == 404 or self._is_model_not_found(response):
            raise IndexError(f"model {self._model} not pulled — run: ollama pull {self._model}")
        if response.status_code >= 500:
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")
        if response.status_code >= 400:
            # 4xx outside the model-not-found shape is still operator-facing.
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise IndexError(f"ollama probe response is not valid JSON: {exc}") from exc
        vectors = body.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise IndexError("ollama probe returned malformed 'embeddings' array")
        first = vectors[0]
        if not isinstance(first, list) or len(first) != EMBED_DIM:
            actual = len(first) if isinstance(first, list) else "unknown"
            raise IndexError(
                f"ollama probe vector has dim {actual}; expected {EMBED_DIM} "
                f"(model {self._model} may be pulled at a different size)"
            )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _is_model_not_found(response: httpx.Response) -> bool:
        """Return True if the response body looks like Ollama's model-not-found shape.

        Ollama's published shape on a missing model is HTTP 404 with a
        JSON body ``{"error": "model 'xxx' not found"}``. Some Ollama
        releases return 4xx with a similar message — we sniff the body
        case-insensitively for ``"not found"`` as defense-in-depth.
        """
        try:
            body = response.json()
        except ValueError:
            text = response.text or ""
            return "not found" in text.lower()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and "not found" in error.lower():
                return True
        return False


class OpenRouterEmbedder:
    """Audited OpenRouter embedder pinned to one upstream provider."""

    def __init__(
        self,
        *,
        store: Path,
        model: str,
        upstream: str,
        api_key: str,
        base_url: str = OPENROUTER_BASE_URL,
        dim: int = EMBED_DIM,
        timeout: float = 120.0,
        max_batch: int = REMOTE_EMBED_MAX_BATCH,
        max_concurrency: int = REMOTE_EMBED_MAX_CONCURRENCY,
        transport: httpx.BaseTransport | None = None,
        purpose: Purpose = "document",
        retry: RetryPolicy | None = None,
        pacer: RequestPacer | None = None,
        _sleep: Callable[[float], None] = time.sleep,
        _rng: Callable[[], float] | None = None,
        _clock: Callable[[], float] = time.monotonic,
        _now: Callable[[], datetime] | None = None,
    ) -> None:
        """``purpose`` labels requests for a shared ``pacer`` only; the request body is the
        same for documents and queries. ``retry`` defaults to :class:`RetryPolicy`'s
        defaults. ``pacer`` admits every physical attempt, retries included."""
        if max_batch < 1:
            raise IndexError(f"max_batch must be at least 1; got {max_batch}")
        if max_concurrency < 1:
            raise IndexError(f"max_concurrency must be at least 1; got {max_concurrency}")
        self._store = store
        self._model = model
        self._upstream = upstream
        self._base_url = base_url.rstrip("/")
        self._dim = dim
        self._max_batch = max_batch
        self.max_concurrency = max_concurrency
        self._timeout = timeout
        self._transport = transport
        self._thread_local = threading.local()
        self._clients_lock = threading.Lock()
        self._clients: list[httpx.Client] = []
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._purpose: Purpose = purpose
        self._retry = retry if retry is not None else RetryPolicy()
        self._pacer = pacer
        self._sleep = _sleep
        self._rng = _rng
        self._clock = _clock
        self._now = _now

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed unchanged inputs in order within count and aggregate request-size budgets.

        The size bound is in UTF-8 bytes (:func:`request_units`), conservative for the
        pinned Qwen byte-level BPE; it is not a provider-reported token count and not a
        rate. Validate all inputs before egress; one oversized input cannot be split
        without changing its vector, so refuse it rather than truncate or silently alter
        identity. A request that fails raises :class:`palace.index.ProviderCallError`
        whose ``completed_inputs`` counts the leading inputs already answered; their
        vectors are not returned.
        """
        if not texts:
            return []
        inputs = list(texts)
        bounds = [request_units(text) for text in inputs]
        for index, bound in enumerate(bounds):
            if bound > REMOTE_EMBED_MAX_TOKEN_BUDGET:
                raise IndexError(
                    f"openrouter input #{index} exceeds the conservative request token budget "
                    f"({bound} > {REMOTE_EMBED_MAX_TOKEN_BUDGET}); shorten the input upstream"
                )
        vectors: list[list[float]] = []
        total = len(inputs)
        start = 0
        while start < total:
            # Prevalidation guarantees the first input fits; advance locally.
            end = start + 1
            budget = bounds[start]
            while (
                end < total
                and end - start < self._max_batch
                and budget + bounds[end] <= REMOTE_EMBED_MAX_TOKEN_BUDGET
            ):
                budget += bounds[end]
                end += 1
            batch = inputs[start:end]
            # Each request's audit record names only the chunks it carried.
            with egress_batch_slice(start=start, count=len(batch), total=total):
                vectors.extend(self._embed_one_batch(batch, completed_inputs=start))
            start = end
        return vectors

    def _embed_one_batch(
        self,
        inputs: list[str],
        *,
        completed_inputs: int = 0,
        policy: RetryPolicy | None = None,
    ) -> list[list[float]]:
        """Embed one within-cap batch: every attempt paced, audited and classified."""
        payload: dict[str, Any] = {
            "model": self._model,
            "input": inputs,
            "provider": {
                "order": [self._upstream],
                "allow_fallbacks": False,
                "zdr": True,
                "data_collection": "deny",
            },
        }
        request = PacedRequest(
            provider="openrouter",
            model=self._model,
            operation="embeddings",
            purpose=self._purpose,
            inputs=len(inputs),
            request_bytes=sum(request_units(text) for text in inputs),
        )
        return perform(
            lambda info: self._send(payload=payload, inputs=inputs, info=info),
            request=request,
            policy=policy if policy is not None else self._retry,
            pacer=self._pacer,
            completed_inputs=completed_inputs,
            rng=self._rng,
            clock=self._clock,
            sleep=self._sleep,
        )

    def _send(
        self, *, payload: dict[str, Any], inputs: list[str], info: AttemptInfo
    ) -> list[list[float]]:
        """Make one audited attempt; raise :class:`AttemptFailure` to classify a failure."""
        response, body, failure = self._attempt(payload=payload, inputs=inputs, info=info)
        if failure is not None:
            raise failure
        assert response is not None
        if body is None:
            raise _invalid("openrouter response is not a JSON object", "response-not-object")
        observed = body.get("provider")
        if observed != self._upstream:
            # The observed identity is provider-controlled; it is never echoed.
            raise _invalid(
                f"openrouter upstream divergence: configured='{self._upstream}' was not the "
                "upstream that answered — build aborted",
                "upstream-divergence",
            )
        try:
            return _parse_openrouter_vectors(body, expected_count=len(inputs), dim=self._dim)
        except IndexError as exc:
            raise _invalid(str(exc), "response-invalid") from None

    def _attempt(
        self, *, payload: dict[str, Any], inputs: list[str], info: AttemptInfo
    ) -> tuple[httpx.Response | None, dict[str, Any] | None, AttemptFailure | None]:
        """Make and audit exactly one HTTP attempt, bounded by the scope's remaining time.

        The timeout is per request, never set on the thread's shared client. httpx applies
        it to each connect, write, read and pool wait, not to the attempt's total duration.
        """
        timeout = self._timeout
        if info.remaining_seconds is not None:
            timeout = max(min(timeout, info.remaining_seconds), _MIN_TIMEOUT_SECONDS)
        started = time.monotonic()
        response: httpx.Response | None = None
        body: dict[str, Any] | None = None
        request_error: httpx.RequestError | None = None
        try:
            response = self._client_for_thread().post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=self._headers,
                timeout=timeout,
            )
            try:
                decoded = response.json()
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                body = decoded
        except httpx.RequestError as exc:
            request_error = exc

        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        observed = body.get("provider") if body is not None else None
        observed_text = observed if isinstance(observed, str) else None
        # The audit names the answering upstream only in a short, known shape; anything
        # else is withheld, never written.
        withheld = observed_text is not None and not _SAFE_UPSTREAM.fullmatch(observed_text)
        if withheld:
            observed_text = None
        prompt_tokens, cost_usd = (
            _usage_values(body)
            if response is not None and response.status_code < 400
            else (None, None)
        )
        record = build_record(
            provider="openrouter",
            upstream_configured=self._upstream,
            upstream_observed=observed_text,
            model=self._model,
            dim=self._dim,
            texts=inputs,
            prompt_tokens=prompt_tokens,
            cost_usd=cost_usd,
            http_status=response.status_code if response is not None else None,
            latency_ms=latency_ms,
            extra={
                "attempt": info.number,
                "pacer_wait_ms": max(0, round(info.pacer_wait_seconds * 1000)),
                "backoff_ms": max(0, round(info.backoff_seconds * 1000)),
                **({"upstream_observed_withheld": True} if withheld else {}),
            },
        )
        failure = (
            _request_failure(request_error)
            if request_error is not None
            else _status_failure(response, len(inputs), now=self._now)
            if response is not None and response.status_code >= 400
            else None
        )
        usage = current_usage()
        if usage is not None:
            # From the values the audit record carries, so the two agree, and before appending it:
            # the request has been made whether or not its record can be written.
            usage.record(
                ok=failure is None,
                prompt_tokens=prompt_tokens,
                cost_usd=cost_usd,
                throttled=failure is not None and failure.kind == "throttled",
                indeterminate=failure is not None and failure.delivery == "unknown",
            )
        try:
            append_record(store=self._store, record=record)
        except OSError as exc:
            raise IndexError(f"cannot append cloud-egress audit record: {exc}") from exc
        return response, body, failure

    def probe(self) -> None:
        """Verify reachability, pin enforcement, and output dimension once.

        A reachability probe has no completed batch work to preserve, so it does
        not inherit the build request's retry budget or its multi-minute wait.
        """
        self._embed_one_batch(
            [_PROBE_INPUT], policy=dataclasses.replace(self._retry, max_attempts=1)
        )

    def close(self) -> None:
        """Close every per-thread HTTP client this embedder constructed."""
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()

    def _client_for_thread(self) -> httpx.Client:
        client = getattr(self._thread_local, "client", None)
        if isinstance(client, httpx.Client):
            return client
        client = _build_client(timeout=self._timeout, transport=self._transport)
        self._thread_local.client = client
        with self._clients_lock:
            self._clients.append(client)
        return client


def _usage_values(body: dict[str, Any] | None) -> tuple[int | None, float | None]:
    if body is None:
        return None, None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    cost = usage.get("cost")
    prompt_value = int(prompt) if isinstance(prompt, int) and not isinstance(prompt, bool) else None
    cost_value = (
        float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    )
    return prompt_value, cost_value


def _is_retryable_status(status: int) -> bool:
    """Retry HTTP 429 and the complete 5xx range."""
    return status == 429 or REMOTE_EMBED_RETRY_STATUS_MIN <= status < 600


# httpx refuses a zero timeout as "no timeout"; a scope with almost no time left still
# gets a finite, tiny bound rather than an unbounded one.
_MIN_TIMEOUT_SECONDS: float = 0.001

# Diagnostics are library-owned: a failure names the HTTP status or the transport
# exception's type, never a response body, a provider message, transport error text or
# the identity a response claims.
_SAFE_UPSTREAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")

# Transport failures: (kind, delivery). Connect-phase failures never sent the request;
# anything after the request started writing may have reached the provider.
_REQUEST_ERRORS: tuple[tuple[type[httpx.RequestError], FailureKind, Delivery], ...] = (
    (httpx.ConnectTimeout, "timeout", "not-sent"),
    (httpx.PoolTimeout, "timeout", "not-sent"),
    (httpx.ConnectError, "connection", "not-sent"),
    (httpx.TimeoutException, "timeout", "unknown"),
)


def _request_failure(error: httpx.RequestError) -> AttemptFailure:
    """Classify a transport failure; every one is retryable, as before."""
    kind: FailureKind = "connection"
    delivery: Delivery = "unknown"
    for error_type, error_kind, error_delivery in _REQUEST_ERRORS:
        if isinstance(error, error_type):
            kind, delivery = error_kind, error_delivery
            break
    return AttemptFailure(
        f"openrouter request failed: {type(error).__name__}",
        kind=kind,
        delivery=delivery,
        code=type(error).__name__,
        retryable=True,
    )


def _status_failure(
    response: httpx.Response, count: int, *, now: Callable[[], datetime] | None = None
) -> AttemptFailure:
    """Classify an HTTP refusal: 429 is a throttle; 503 a refusal to serve; other 5xx are
    indeterminate (the upstream may have done the work); other 4xx refuse at once."""
    status = response.status_code
    kind: FailureKind
    delivery: Delivery
    if status == 429:
        kind, delivery = "throttled", "rejected"
    elif status == 503:
        kind, delivery = "unavailable", "rejected"
    elif status == 504:
        kind, delivery = "timeout", "unknown"
    elif status >= REMOTE_EMBED_RETRY_STATUS_MIN:
        kind, delivery = "unavailable", "unknown"
    else:
        kind, delivery = "rejected", "rejected"
    return AttemptFailure(
        f"openrouter returned {status} for a {count}-input batch",
        kind=kind,
        delivery=delivery,
        code=str(status),
        retryable=_is_retryable_status(status),
        retry_after=parse_retry_after(
            response.headers.get("retry-after"), date=response.headers.get("date"), now=now
        ),
    )


def _invalid(message: str, code: str) -> AttemptFailure:
    """An answered but unusable response: the provider did the work; never retried."""
    return AttemptFailure(
        message, kind="response-invalid", delivery="processed", code=code, retryable=False
    )


def _parse_openrouter_vectors(
    body: dict[str, Any], *, expected_count: int, dim: int
) -> list[list[float]]:
    data = body.get("data")
    if not isinstance(data, list):
        raise IndexError("openrouter response missing 'data' array")
    if len(data) != expected_count:
        raise IndexError(f"openrouter returned {len(data)} vectors for {expected_count} inputs")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise IndexError(f"openrouter data entry #{index} is not an object")
        entries.append(item)
    if entries and all(isinstance(item.get("index"), int) for item in entries):
        entries.sort(key=lambda item: int(item["index"]))
    vectors: list[list[float]] = []
    for index, item in enumerate(entries):
        vector = item.get("embedding")
        if not isinstance(vector, list):
            raise IndexError(f"openrouter vector #{index} is not a list")
        if len(vector) != dim:
            raise IndexError(f"openrouter vector #{index} has dim {len(vector)}; expected {dim}")
        try:
            vectors.append([float(value) for value in vector])
        except (TypeError, ValueError) as exc:
            raise IndexError(f"openrouter vector #{index} contains a non-number") from exc
    return vectors
