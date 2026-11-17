"""Audited AWS Bedrock Cohere Embed v4 embedder on a direct regional runtime endpoint.

Bedrock is third-party inference: the store's published-corpus assertion and the
personal-store and personal-vault refusals are applied by the caller through
:func:`palace.index.embedder_config.assert_remote_corpus_boundary` before an embedder
is used. Construction is inert. The first :meth:`BedrockEmbedder.probe` or
:meth:`BedrockEmbedder.embed` resolves the AWS SDK credential chain or a Bedrock API
key (bearer token), then reads the selector Region's ``GetAccountDataRetention`` mode
with that same authentication and refuses unless it is exactly ``none``. That check
reads one Region's configured mode at one moment; it does not observe what the service
retains (``briefs/bedrock-embedding-provider.md``).

SDK retries are disabled and configured endpoint-URL overrides are ignored, so every
request goes to the selector Region's standard endpoint and every attempt is palace's:
audited, counted and visible. Each logical request runs through
:func:`palace.index.pacing.perform`: an optional shared
:class:`~palace.index.pacing.RequestPacer` admits every embedding attempt, a
:class:`~palace.index.pacing.RetryPolicy` spaces retries with jitter under any
``Retry-After`` floor, and a failure ends in a typed
:class:`palace.index.ProviderCallError` whose ``delivery`` keeps every earlier
indeterminate attempt. Inside a :func:`~palace.index.pacing.call_scope`, the wait for the
initialization lock, pacer and backoff stop at the deadline, no attempt starts after it,
and each request's socket read timeout is lowered to the remaining time (botocore's
per-request ``read_timeout``; the shared client is never changed). An attempt already in
flight is not otherwise interrupted, and SDK credential resolution is not bounded. No
corpus text, query text or credential is written to the audit.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from palace.index._errors import (
    AttemptRecord,
    Delivery,
    FailureKind,
    IndexError,
    ProviderCallError,
)
from palace.index.config import (
    BEDROCK_CONNECT_TIMEOUT_SECONDS,
    BEDROCK_EMBED_MAX_CONCURRENCY,
    BEDROCK_MAX_BATCH,
    BEDROCK_READ_TIMEOUT_SECONDS,
    BEDROCK_REQUEST_BUDGET,
)
from palace.index.egress import append_record, build_record, current_usage, egress_batch_slice
from palace.index.embedder import request_units
from palace.index.embedder_config import EmbedPurpose
from palace.index.pacing import (
    AttemptFailure,
    AttemptInfo,
    PacedRequest,
    RequestPacer,
    RetryPolicy,
    WaitInterrupted,
    parse_retry_after,
    perform,
    remaining_seconds,
    scoped_lock,
)

__all__ = ["BEDROCK_RETRYABLE_CODES", "BedrockEmbedder", "open_bedrock_client"]

# The pinned runtime service model's throttling, unavailability, internal, model-timeout
# and model-not-ready errors plus botocore's connection failures, each with its failure kind
# and what its answer says about delivery: a throttle or an explicit refusal to serve was not
# performed; an internal error, a model timeout or a read that broke off may have been; a
# connect-phase failure never sent the request. Everything else (ValidationException,
# AccessDeniedException, ...) refuses on its first attempt.
_RETRYABLE: dict[str, tuple[FailureKind, Delivery]] = {
    "ThrottlingException": ("throttled", "rejected"),
    "ServiceUnavailableException": ("unavailable", "rejected"),
    "ModelNotReadyException": ("unavailable", "rejected"),
    "InternalServerException": ("unavailable", "unknown"),
    "ModelTimeoutException": ("timeout", "unknown"),
    "EndpointConnectionError": ("connection", "not-sent"),
    "ConnectTimeoutError": ("timeout", "not-sent"),
    "ReadTimeoutError": ("timeout", "unknown"),
    "ConnectionClosedError": ("connection", "unknown"),
}
BEDROCK_RETRYABLE_CODES = frozenset(_RETRYABLE)
# Service codes the pinned InvokeModel and GetAccountDataRetention models document as
# refusing the request itself: missing permission, a missing resource, invalid input, or
# an exceeded account quota. Any other non-retryable service code may have been processed:
# ModelErrorException is documented as failing "due to an error while processing the
# model", and a code the pinned models do not name is not known to be a refusal.
_DEFINITE_REFUSALS = frozenset(
    {
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ValidationException",
        "ServiceQuotaExceededException",
    }
)
# Every service code the pinned InvokeModel and GetAccountDataRetention models declare.
_KNOWN_CODES = BEDROCK_RETRYABLE_CODES | _DEFINITE_REFUSALS | {"ModelErrorException"}
# A floor so a scope with almost no time left still gets a finite, positive read bound.
_MIN_READ_TIMEOUT_SECONDS = 0.001
_REQUIRED_RETENTION_MODE = "none"
# Diagnostics are library-owned: a failure names the provider, operation, Region and a
# validated code, never the service's message, a transport error's text or a response
# value outside a short known shape. A service code the pinned models do not declare is
# replaced, so a code cannot carry provider-chosen text into a message or the audit.
_UNRECOGNIZED_CODE = "UnrecognizedServiceError"
_SAFE_MODE = re.compile(r"[a-z][a-z_]{0,31}")
# RFC 6750 section 2.1 ``b64token`` (token68): the only bearer credential syntax Palace admits.
_TOKEN68 = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_sleep = time.sleep  # the retry backoff; a test replaces this seam, not the time module


def _utcnow() -> datetime:
    """The clock an HTTP-date ``Retry-After`` falls back to without a readable ``Date``;
    a test replaces this module seam."""
    return datetime.now(UTC)


def _backoff_sleep(seconds: float) -> None:
    """Sleep through the module seam, looked up at call time so a test's replacement holds."""
    _sleep(seconds)


def open_bedrock_client(service: str, *, profile: str | None, region: str) -> Any:
    """Open a ``bedrock`` or ``bedrock-runtime`` client bound to ``region``'s endpoint.

    The client authenticates however the SDK resolves it: SigV4 from the credential
    chain, or a Bedrock API key in the SDK's bearer-token variable for the service's
    signing name (``AWS_BEARER_TOKEN_BEDROCK``), which the SDK prefers when present.
    Refuses before any request when neither resolves, or when that variable is set but
    blank. Admission is not authorization: the caller's retention read still decides.
    SDK retries are off (one total attempt) and ambient endpoint-URL overrides
    (``AWS_ENDPOINT_URL*`` and a profile's endpoint setting) are ignored.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client(
            service,
            config=Config(
                retries={"mode": "standard", "total_max_attempts": 1},
                ignore_configured_endpoint_urls=True,
                connect_timeout=BEDROCK_CONNECT_TIMEOUT_SECONDS,
                read_timeout=BEDROCK_READ_TIMEOUT_SECONDS,
            ),
        )
        client.meta.events.register("before-call", _bound_read_timeout)
        try:
            token = _bearer_token_state(client)
            if token == "blank":
                raise IndexError(
                    f"bedrock bearer token variable {_bearer_variable(client)} is set but "
                    "blank; unset it or provide a Bedrock API key"
                )
            if token == "malformed":
                raise IndexError(
                    f"bedrock bearer token in {_bearer_variable(client)} is not a valid "
                    "bearer token (RFC 6750 token68 characters only, no whitespace); "
                    "the value is not shown"
                )
            if token == "absent" and session.get_credentials() is None:
                raise IndexError(
                    "bedrock credentials unavailable: configure the AWS SDK credential chain "
                    "(profile, environment or instance role) or a Bedrock API key in "
                    f"{_bearer_variable(client)}"
                )
        except BaseException:
            client.close()
            raise
        return client
    except BotoCoreError as exc:
        raise IndexError(
            f"bedrock {service} client could not be opened: {type(exc).__name__}"
        ) from None


def _bound_read_timeout(context: dict[str, Any], **_: Any) -> None:
    """Lower this one request's socket read timeout to the active scope's remaining time.

    Runs on the calling thread, so it reads that call's own scope; it writes only the
    request's context, never the client shared by concurrent calls.
    """
    remaining = remaining_seconds()
    if remaining is not None:
        context["read_timeout"] = max(
            _MIN_READ_TIMEOUT_SECONDS, min(BEDROCK_READ_TIMEOUT_SECONDS, remaining)
        )


def _retry_hint(exc: BaseException) -> float | None:
    """The provider's ``Retry-After`` on a failed response, in seconds, if it gave one."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    headers = response.get("ResponseMetadata", {}).get("HTTPHeaders")
    if not isinstance(headers, dict):
        return None
    return parse_retry_after(headers.get("retry-after"), date=headers.get("date"), now=_utcnow)


def _classify(code: str, exc: BaseException) -> tuple[FailureKind, Delivery, bool]:
    """``(kind, delivery, retryable)`` for one failed attempt.

    Only the documented refusals in ``_DEFINITE_REFUSALS`` are definitely rejected. Every
    other non-retryable service code (ModelErrorException, or one the pinned models do not
    name) and every other botocore failure (a stream that broke off, an SSL failure) is
    treated as possibly processed, so uncertainty is over-reported rather than lost. None of
    them is retried.
    """
    from botocore.exceptions import ClientError

    if code in _RETRYABLE:
        kind, delivery = _RETRYABLE[code]
        return kind, delivery, True
    if code in _DEFINITE_REFUSALS:
        return "rejected", "rejected", False
    if isinstance(exc, ClientError):
        return "unavailable", "unknown", False
    return "connection", "unknown", False


def _bearer_variable(client: Any) -> str:
    """The SDK's bearer-token environment variable for this client's signing name."""
    signing_name = str(client.meta.service_model.signing_name)
    return "AWS_BEARER_TOKEN_" + signing_name.replace("-", "_").replace(" ", "_").upper()


def _bearer_token_state(client: Any) -> str:
    """``present``, ``blank``, ``malformed`` or ``absent``: the SDK bearer token this client
    would sign with.

    The SDK signs with an environment bearer token only for a service whose model admits
    ``smithy.api#httpBearerAuth``; for any other service the variable is irrelevant. A
    token outside the RFC 6750 ``token68`` grammar is refused here because the SDK would
    copy it verbatim into the ``Authorization`` header, and an invalid header value is
    echoed in the HTTP library's error. The token value never leaves this function.
    """
    from botocore.utils import get_token_from_environment

    model = client.meta.service_model
    if "smithy.api#httpBearerAuth" not in (model.metadata.get("auth") or ()):
        return "absent"
    token = get_token_from_environment(model.signing_name)
    if token is None:
        return "absent"
    if not token.strip():
        return "blank"
    return "present" if _TOKEN68.fullmatch(token) else "malformed"


def _error_code(exc: BaseException) -> str | None:
    """The AWS error code of a botocore failure; ``None`` for anything else."""
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if not code:
            return "ClientError"
        text = str(code)
        return text if text in _KNOWN_CODES else _UNRECOGNIZED_CODE
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
        retry: RetryPolicy | None = None,
        pacer: RequestPacer | None = None,
        _rng: Callable[[], float] | None = None,
    ) -> None:
        """``retry`` defaults to :class:`RetryPolicy`'s defaults. ``pacer`` admits every
        ``InvokeModel`` attempt, retries included; the once-per-instance retention read is a
        control-plane request outside any embedding quota and is not paced."""
        self.max_concurrency = BEDROCK_EMBED_MAX_CONCURRENCY
        self._store = store
        self._model = model
        self._region = region
        self._dim = dim
        self._profile = profile
        self._purpose: EmbedPurpose = purpose
        self._input_type = "search_document" if purpose == "document" else "search_query"
        self._provider = f"bedrock:{region}"
        self._retry = retry if retry is not None else RetryPolicy()
        self._pacer = pacer
        self._rng = _rng
        self._lock = threading.Lock()
        self._runtime: Any = None

    # ---------------------------------------------------------------- API

    def probe(self) -> None:
        """Resolve credentials and run the retention check; no embedding is requested."""
        self._ready()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` in order, in batches of at most 96 inside the request budget.

        The budget is a per-request size cap in conservative UTF-8 bytes
        (:func:`~palace.index.embedder.request_units`), not tokens and not a rate. Every input
        is validated before any egress: one input above the budget cannot be split without
        changing its vector, so it is refused rather than truncated. A request that fails
        raises :class:`palace.index.ProviderCallError` whose ``completed_inputs`` counts the
        leading inputs already answered; their vectors are not returned.
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
                vectors.extend(self._embed_batch(runtime, batch, completed_inputs=start))
            start = end
        return vectors

    def close(self) -> None:
        with self._lock:
            runtime, self._runtime = self._runtime, None
        if runtime is not None:
            runtime.close()

    # ---------------------------------------------------------------- internals

    def _ready(self) -> Any:
        """Open the runtime client once, after a passing same-Region retention check.

        A caller queued behind another's initialization waits no longer than its own scope;
        stopping there is a typed ``deadline``/``cancelled`` failure with nothing sent.
        """
        with ExitStack() as held:
            try:
                held.enter_context(scoped_lock(self._lock))
            except WaitInterrupted as signal:
                raise ProviderCallError(
                    f"{self._provider} initialization stopped by {signal.reason} while "
                    "waiting for another caller after 0 attempt(s)",
                    provider=self._provider,
                    operation="initialize",
                    kind=signal.reason,
                    delivery="not-sent",
                    code=signal.reason,
                    attempts=0,
                    stop="interrupted",
                    history=(AttemptRecord(signal.reason, "not-sent", signal.reason, None),),
                ) from signal.__cause__
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
                    shown = (
                        repr(mode)
                        if isinstance(mode, str) and _SAFE_MODE.fullmatch(mode)
                        else "an unrecognized value"
                    )
                    raise IndexError(
                        f"bedrock retention mode in {self._region} is {shown}; palace "
                        f"requires {_REQUIRED_RETENTION_MODE!r}"
                    )
                self._runtime = open_bedrock_client(
                    "bedrock-runtime", profile=self._profile, region=self._region
                )
            return self._runtime

    def _embed_batch(
        self, runtime: Any, batch: list[str], *, completed_inputs: int
    ) -> list[list[float]]:
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
            completed_inputs=completed_inputs,
            validate=lambda answer: self._vectors(answer, count=len(batch)),
        )
        vectors: list[list[float]] = response["vectors"]
        return vectors

    def _call(
        self,
        operation: str,
        method: Any,
        *,
        texts: list[str],
        completed_inputs: int = 0,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Make one logical call; each attempt is its own audited, classified request.

        ``validate`` checks the complete answer inside the attempt, so an unusable answer
        ends the request typed as ``response-invalid`` with delivery ``processed``.
        """
        embedding = operation == "InvokeModel"

        def send(info: AttemptInfo) -> dict[str, Any]:
            started = time.monotonic()
            try:
                response = method()
                raw = response["body"].read() if embedding else None
            except Exception as exc:  # noqa: BLE001 — only botocore failures are translated
                code = _error_code(exc)
                if code is None:
                    raise
                kind, delivery, retryable = _classify(code, exc)
                self._audit(
                    operation,
                    texts,
                    code,
                    info,
                    started,
                    _http_status(exc),
                    throttled=kind == "throttled",
                    indeterminate=delivery == "unknown",
                )
                raise AttemptFailure(
                    f"bedrock {operation} failed in {self._region}: {code}",
                    kind=kind,
                    delivery=delivery,
                    code=code,
                    retryable=retryable,
                    retry_after=_retry_hint(exc),
                ) from None
            self._audit(operation, texts, "ok", info, started, _http_status(response))
            result = dict(response)
            if raw is not None:
                result["body"] = raw
            if validate is not None:
                try:
                    result["vectors"] = validate(result)
                except IndexError as exc:
                    raise AttemptFailure(
                        str(exc),
                        kind="response-invalid",
                        delivery="processed",
                        code="response-invalid",
                        retryable=False,
                    ) from None
            return result

        request = PacedRequest(
            provider=self._provider,
            model=self._model,
            operation=operation,
            purpose=self._purpose,
            inputs=len(texts),
            request_bytes=sum(request_units(text) for text in texts),
        )
        return perform(
            send,
            request=request,
            policy=self._retry,
            pacer=self._pacer if embedding else None,
            completed_inputs=completed_inputs,
            rng=self._rng,
            sleep=_backoff_sleep,
        )

    def _audit(
        self,
        operation: str,
        texts: list[str],
        status: str,
        info: AttemptInfo,
        started: float,
        http_status: int | None,
        *,
        throttled: bool = False,
        indeterminate: bool = False,
    ) -> None:
        embedding = operation == "InvokeModel"
        extra: dict[str, Any] = {
            "operation": operation,
            "attempt": info.number,
            "status": status,
            "pacer_wait_ms": max(0, round(info.pacer_wait_seconds * 1000)),
            "backoff_ms": max(0, round(info.backoff_seconds * 1000)),
        }
        if embedding:
            extra["input_type"] = self._input_type
        else:
            extra["region"] = self._region
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
            extra=extra,
        )
        usage = current_usage()
        if embedding and usage is not None:
            # Before the append: the request has been made whether or not its record is written.
            usage.record(
                ok=status == "ok",
                prompt_tokens=None,
                cost_usd=None,
                throttled=throttled,
                indeterminate=indeterminate,
            )
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
