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
retry. Tests use ``httpx.MockTransport``; only the smoke needs live Ollama.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from palace.index._errors import IndexError
from palace.index.config import (
    EMBED_DIM,
    EMBEDDER_MODEL,
    OLLAMA_BASE_URL,
    OPENROUTER_BASE_URL,
    REMOTE_EMBED_BACKOFF_BASE_SECONDS,
    REMOTE_EMBED_BACKOFF_MAX_SECONDS,
    REMOTE_EMBED_MAX_ATTEMPTS,
    REMOTE_EMBED_MAX_BATCH,
    REMOTE_EMBED_MAX_CONCURRENCY,
    REMOTE_EMBED_RETRY_STATUS_MIN,
)
from palace.index.egress import append_record, build_record, current_usage, egress_batch_slice

__all__ = ["Embedder", "OllamaEmbedder", "OpenRouterEmbedder"]


# A short literal palace uses for the startup probe. Distinctive enough
# that a stray hit in the chunks table would be obviously synthetic.
_PROBE_INPUT: str = "__palace_probe__"


def _build_client(*, timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    """Construct an HTTP client through the suite's hermeticity seam."""
    return httpx.Client(timeout=timeout, transport=transport)


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
        max_attempts: int = REMOTE_EMBED_MAX_ATTEMPTS,
        backoff_base_seconds: float = REMOTE_EMBED_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = REMOTE_EMBED_BACKOFF_MAX_SECONDS,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_batch < 1:
            raise IndexError(f"max_batch must be at least 1; got {max_batch}")
        if max_attempts < 1:
            raise IndexError(f"max_attempts must be at least 1; got {max_attempts}")
        if max_concurrency < 1:
            raise IndexError(f"max_concurrency must be at least 1; got {max_concurrency}")
        if backoff_base_seconds < 0 or backoff_max_seconds < 0:
            raise IndexError("remote embed backoff values must be non-negative")
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
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` serially, splitting at the upstream's input cap.

        The cap is a provider limit (see
        :data:`palace.index.config.REMOTE_EMBED_MAX_BATCH`), so the split
        happens here rather than at each call site: every caller gets it, and
        each batch remains exactly one request and one audit record. Vectors
        are concatenated in input order, so the return value is
        indistinguishable from an unsplit call apart from batch-composition
        float jitter, which the identical-output invariant already tolerates.
        """
        if not texts:
            return []
        inputs = list(texts)
        if len(inputs) <= self._max_batch:
            return self._embed_one_batch(inputs)
        vectors: list[list[float]] = []
        total = len(inputs)
        for start in range(0, total, self._max_batch):
            batch = inputs[start : start + self._max_batch]
            # Each request's audit record names only the chunks it carried.
            with egress_batch_slice(start=start, count=len(batch), total=total):
                vectors.extend(self._embed_one_batch(batch))
        return vectors

    def _embed_one_batch(
        self, inputs: list[str], *, max_attempts: int | None = None
    ) -> list[list[float]]:
        """Embed one within-cap batch, audit it, and verify the echoed upstream."""
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
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        for attempt in range(1, attempt_limit + 1):
            response, body, request_error = self._attempt(payload=payload, inputs=inputs)
            if request_error is not None:
                if attempt < attempt_limit:
                    self._sleep(self._retry_delay(attempt=attempt, response=None))
                    continue
                raise IndexError(f"openrouter request failed: {request_error}") from request_error

            assert response is not None
            if response.status_code >= 400:
                if _is_retryable_status(response.status_code) and attempt < attempt_limit:
                    self._sleep(self._retry_delay(attempt=attempt, response=response))
                    continue
                raise IndexError(
                    f"openrouter returned {response.status_code} for a {len(inputs)}-input batch: "
                    f"{response.text.strip()}"
                )
            if body is None:
                raise IndexError("openrouter response is not a JSON object")
            observed = body.get("provider")
            observed_text = observed if isinstance(observed, str) else None
            if observed_text != self._upstream:
                found = observed_text if observed_text is not None else "none"
                raise IndexError(
                    "openrouter upstream divergence: "
                    f"configured='{self._upstream}' observed='{found}' — build aborted"
                )
            return _parse_openrouter_vectors(body, expected_count=len(inputs), dim=self._dim)
        raise AssertionError("bounded retry loop exited without returning or raising")

    def _attempt(
        self, *, payload: dict[str, Any], inputs: list[str]
    ) -> tuple[httpx.Response | None, dict[str, Any] | None, httpx.RequestError | None]:
        """Make and audit exactly one HTTP attempt."""
        started = time.monotonic()
        response: httpx.Response | None = None
        body: dict[str, Any] | None = None
        request_error: httpx.RequestError | None = None
        try:
            response = self._client_for_thread().post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=self._headers,
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
        )
        usage = current_usage()
        if usage is not None:
            # From the values the audit record carries, so the two agree, and before appending it:
            # the request has been made whether or not its record can be written.
            usage.record(
                ok=response is not None and response.status_code < 400,
                prompt_tokens=prompt_tokens,
                cost_usd=cost_usd,
            )
        try:
            append_record(store=self._store, record=record)
        except OSError as exc:
            raise IndexError(f"cannot append cloud-egress audit record: {exc}") from exc
        return response, body, request_error

    def _retry_delay(self, *, attempt: int, response: httpx.Response | None) -> float:
        exponential: float = self._backoff_base_seconds * (2 ** (attempt - 1))
        retry_after = _retry_after_seconds(response)
        requested: float = max(exponential, retry_after) if retry_after is not None else exponential
        return float(min(requested, self._backoff_max_seconds))

    def probe(self) -> None:
        """Verify reachability, pin enforcement, and output dimension once.

        A reachability probe has no completed batch work to preserve, so it does
        not inherit the build request's retry budget or its multi-minute wait.
        """
        self._embed_one_batch([_PROBE_INPUT], max_attempts=1)

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


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            now = parsedate_to_datetime(response.headers["date"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        parsed = (target - now).total_seconds()
    return max(0.0, parsed)


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
