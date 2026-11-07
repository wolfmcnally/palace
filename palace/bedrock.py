"""Audited AWS Bedrock Cohere Embed v4 embedder on a direct regional runtime endpoint.

Bedrock is third-party inference: the store's published-corpus assertion and the
personal-store and personal-vault refusals are applied by the caller through
:func:`palace.index.embedder_config.assert_remote_corpus_boundary` before an embedder
is used. Construction is inert. The first :meth:`BedrockEmbedder.probe` or
:meth:`BedrockEmbedder.embed` resolves the AWS SDK credential chain, then reads the
selector Region's ``GetAccountDataRetention`` mode and refuses unless it is exactly
``none``. That check reads one Region's configured mode at one moment; it does not
observe what the service retains (``briefs/bedrock-embedding-provider.md``).

SDK retries are disabled and configured endpoint-URL overrides are ignored, so every
request goes to the selector Region's standard endpoint and every attempt is palace's:
audited, counted and visible. No corpus text, query text or credential is written to
the audit.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from palace.daemons.capture.ids import compute_record_id
from palace.index._errors import IndexError
from palace.index.config import (
    BEDROCK_EMBED_MAX_CONCURRENCY,
    BEDROCK_MAX_BATCH,
    BEDROCK_REQUEST_BUDGET,
    REMOTE_EMBED_BACKOFF_BASE_SECONDS,
    REMOTE_EMBED_BACKOFF_MAX_SECONDS,
    REMOTE_EMBED_MAX_ATTEMPTS,
)
from palace.index.egress import append_record, build_record, current_usage, egress_batch_slice
from palace.index.embedder import request_units
from palace.index.embedder_config import EmbedPurpose

__all__ = ["BEDROCK_RETRYABLE_CODES", "BedrockEmbedder", "open_bedrock_client"]

# The pinned runtime service model's throttling, unavailability, internal, model-timeout
# and model-not-ready errors plus botocore's connection failures. Everything else
# (ValidationException, AccessDeniedException, ...) refuses on its first attempt.
BEDROCK_RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
    }
)
_REQUIRED_RETENTION_MODE = "none"
_sleep = time.sleep  # the retry backoff; a test replaces this seam, not the time module


def open_bedrock_client(service: str, *, profile: str | None, region: str) -> Any:
    """Open a ``bedrock`` or ``bedrock-runtime`` client bound to ``region``'s endpoint.

    Refuses before any request when the SDK credential chain resolves nothing. SDK
    retries are off (one total attempt) and ambient endpoint-URL overrides
    (``AWS_ENDPOINT_URL*`` and a profile's endpoint setting) are ignored.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        if session.get_credentials() is None:
            raise IndexError(
                "bedrock credentials unavailable: configure the AWS SDK credential chain "
                "(profile, environment or instance role)"
            )
        return session.client(
            service,
            config=Config(
                retries={"mode": "standard", "total_max_attempts": 1},
                ignore_configured_endpoint_urls=True,
            ),
        )
    except BotoCoreError as exc:
        raise IndexError(f"bedrock {service} client could not be opened: {exc}") from None


def _error_code(exc: BaseException) -> str | None:
    """The AWS error code of a botocore failure; ``None`` for anything else."""
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return None


def _http_status(source: object) -> int | None:
    """The HTTP status a botocore response or ``ClientError`` carries, if any."""
    response = source if isinstance(source, dict) else getattr(source, "response", None)
    if not isinstance(response, dict):
        return None
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Message"):
            return str(error["Message"])
    return str(exc)


class BedrockEmbedder:
    """Cohere Embed v4 through ``InvokeModel`` for one store, Region and purpose."""

    def __init__(
        self,
        *,
        store: Path,
        model: str,
        region: str,
        dim: int,
        profile: str | None,
        purpose: EmbedPurpose,
    ) -> None:
        self.max_concurrency = BEDROCK_EMBED_MAX_CONCURRENCY
        self._store = store
        self._model = model
        self._region = region
        self._dim = dim
        self._profile = profile
        self._input_type = "search_document" if purpose == "document" else "search_query"
        self._provider = f"bedrock:{region}"
        self._lock = threading.Lock()
        self._runtime: Any = None

    # ---------------------------------------------------------------- API

    def probe(self) -> None:
        """Resolve credentials and run the retention check; no embedding is requested."""
        self._ready()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` in order, in batches of at most 96 inside the request budget.

        Every input is validated before any egress: one input above the budget cannot be
        split without changing its vector, so it is refused rather than truncated.
        """
        if not texts:
            return []
        inputs = list(texts)
        bounds = [request_units(text) for text in inputs]
        for index, bound in enumerate(bounds):
            if bound > BEDROCK_REQUEST_BUDGET:
                raise IndexError(
                    f"bedrock input #{index} exceeds the conservative request budget "
                    f"({bound} > {BEDROCK_REQUEST_BUDGET}); shorten the input upstream"
                )
        runtime = self._ready()
        vectors: list[list[float]] = []
        total = len(inputs)
        start = 0
        while start < total:
            end = start + 1
            budget = bounds[start]
            while (
                end < total
                and end - start < BEDROCK_MAX_BATCH
                and budget + bounds[end] <= BEDROCK_REQUEST_BUDGET
            ):
                budget += bounds[end]
                end += 1
            batch = inputs[start:end]
            with egress_batch_slice(start=start, count=len(batch), total=total):
                vectors.extend(self._embed_batch(runtime, batch))
            start = end
        return vectors

    def close(self) -> None:
        with self._lock:
            runtime, self._runtime = self._runtime, None
        if runtime is not None:
            runtime.close()

    # ---------------------------------------------------------------- internals

    def _ready(self) -> Any:
        """Open the runtime client once, after a passing same-Region retention check."""
        with self._lock:
            if self._runtime is None:
                control = open_bedrock_client("bedrock", profile=self._profile, region=self._region)
                try:
                    response = self._call(
                        "GetAccountDataRetention",
                        lambda: control.get_account_data_retention(),
                        texts=[],
                    )
                finally:
                    control.close()
                mode = response.get("mode")
                if mode != _REQUIRED_RETENTION_MODE:
                    raise IndexError(
                        f"bedrock retention mode in {self._region} is {str(mode)!r}; palace "
                        f"requires {_REQUIRED_RETENTION_MODE!r}"
                    )
                self._runtime = open_bedrock_client(
                    "bedrock-runtime", profile=self._profile, region=self._region
                )
            return self._runtime

    def _embed_batch(self, runtime: Any, batch: list[str]) -> list[list[float]]:
        body = json.dumps(
            {
                "texts": batch,
                "input_type": self._input_type,
                "embedding_types": ["float"],
                "truncate": "NONE",
                "output_dimension": self._dim,
            }
        )
        response = self._call(
            "InvokeModel",
            lambda: runtime.invoke_model(
                modelId=self._model,
                contentType="application/json",
                accept="application/json",
                body=body,
            ),
            texts=batch,
        )
        return self._vectors(response, count=len(batch))

    def _call(self, operation: str, method: Any, *, texts: list[str]) -> dict[str, Any]:
        """Make one logical call; each attempt is its own audited request."""
        for attempt in range(1, REMOTE_EMBED_MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                response = method()
                raw = response["body"].read() if operation == "InvokeModel" else None
            except Exception as exc:  # noqa: BLE001 — only botocore failures are translated
                code = _error_code(exc)
                if code is None:
                    raise
                self._audit(operation, texts, code, attempt, started, _http_status(exc))
                if code in BEDROCK_RETRYABLE_CODES and attempt < REMOTE_EMBED_MAX_ATTEMPTS:
                    _sleep(
                        min(
                            REMOTE_EMBED_BACKOFF_MAX_SECONDS,
                            REMOTE_EMBED_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1),
                        )
                    )
                    continue
                raise IndexError(
                    f"bedrock {operation} failed in {self._region}: {code}: {_message(exc)}"
                ) from None
            self._audit(operation, texts, "ok", attempt, started, _http_status(response))
            result = dict(response)
            if raw is not None:
                result["body"] = raw
            return result
        raise AssertionError("unreachable: the final attempt returns or raises")

    def _audit(
        self,
        operation: str,
        texts: list[str],
        status: str,
        attempt: int,
        started: float,
        http_status: int | None,
    ) -> None:
        embedding = operation == "InvokeModel"
        record = build_record(
            provider=self._provider,
            upstream_configured=self._region,
            upstream_observed=None,
            model=self._model,
            dim=self._dim,
            texts=texts,
            prompt_tokens=None,
            cost_usd=None,
            http_status=http_status,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            kind="cloud_embed_call" if embedding else "bedrock_retention_check",
        )
        record.update(operation=operation, attempt=attempt, status=status)
        if embedding:
            record["input_type"] = self._input_type
        else:
            record["region"] = self._region
        record["id"] = compute_record_id(
            {key: value for key, value in record.items() if key != "id"}
        )
        usage = current_usage()
        if embedding and usage is not None:
            # Before the append: the request has been made whether or not its record is written.
            usage.record(ok=status == "ok", prompt_tokens=None, cost_usd=None)
        try:
            append_record(store=self._store, record=record)
        except OSError:
            raise IndexError("bedrock cannot append its required audit record") from None

    def _vectors(self, response: dict[str, Any], *, count: int) -> list[list[float]]:
        """Validate the complete response before any vector is returned."""
        try:
            body = json.loads(response["body"])
        except (ValueError, UnicodeError):
            raise IndexError("bedrock response is not valid JSON") from None
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if isinstance(embeddings, dict):
            embeddings = embeddings.get("float")
        if not isinstance(embeddings, list):
            raise IndexError("bedrock response has no float embeddings array")
        if len(embeddings) != count:
            raise IndexError(f"bedrock returned {len(embeddings)} vectors for {count} inputs")
        vectors: list[list[float]] = []
        for index, vector in enumerate(embeddings):
            if not isinstance(vector, list) or len(vector) != self._dim:
                size = len(vector) if isinstance(vector, list) else "none"
                raise IndexError(f"bedrock vector #{index} has dim {size}; expected {self._dim}")
            values: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise IndexError(f"bedrock vector #{index} contains a non-number")
                try:
                    number = float(value)
                except OverflowError:
                    raise IndexError(
                        f"bedrock vector #{index} contains a non-finite value"
                    ) from None
                if not math.isfinite(number):
                    raise IndexError(f"bedrock vector #{index} contains a non-finite value")
                values.append(number)
            vectors.append(values)
        return vectors
