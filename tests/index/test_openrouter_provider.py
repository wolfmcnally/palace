"""OpenRouter failure, retry, pacing and deadline proofs through ``httpx.MockTransport``."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from palace.index._errors import IndexError as PalaceError
from palace.index._errors import ProviderCallError, ProviderThrottledError
from palace.index.egress import EmbeddingUsage, usage_scope
from palace.index.embedder import OpenRouterEmbedder, request_units
from palace.index.pacing import call_scope
from tests.test_bedrock_provider import RecordingPacer

UPSTREAM = "Elsewhere"
DIM = 4

# One scripted answer per request: "ok", a status, (status, headers, json body or text), or a
# transport exception (type or instance) to raise.
Step = (
    str | int | tuple[int, dict[str, str], object] | type[httpx.RequestError] | httpx.RequestError
)


class Script:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step: Step = self.steps.pop(0) if self.steps else "ok"
        if isinstance(step, type):
            raise step("fixture", request=request)
        if isinstance(step, httpx.RequestError):
            raise step
        if step == "ok":
            inputs = json.loads(request.content)["input"]
            data = [
                {"index": i, "embedding": [float(i), 1.0, 2.0, 3.0]} for i in range(len(inputs))
            ]
            return httpx.Response(200, json={"data": data, "provider": UPSTREAM})
        if isinstance(step, int):
            return httpx.Response(step, json={"error": {"message": "fixture"}})
        assert isinstance(step, tuple)
        status, headers, body = step
        if isinstance(body, str):
            return httpx.Response(status, headers=headers, text=body)
        return httpx.Response(status, headers=headers, json=body)


def records(store: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((store / "events" / "cloud-egress").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_openrouter_failures_are_typed_paced_and_bounded(tmp_path: Path) -> None:
    """Every OpenRouter failure ends typed with merged delivery; Retry-After never retries
    early; a scope bounds each request's timeout and every wait; a shared pacer sees every
    physical attempt with its byte size; partial success and usage stay truthful."""
    store = tmp_path / "store"
    script = Script()
    sleeps: list[float] = []

    def make(**options: Any) -> OpenRouterEmbedder:
        return OpenRouterEmbedder(
            store=store,
            model="Other/Model",
            upstream=UPSTREAM,
            api_key="synthetic",
            dim=DIM,
            transport=httpx.MockTransport(script),
            _sleep=sleeps.append,
            **options,
        )

    def fails(embedder: OpenRouterEmbedder, texts: list[str]) -> ProviderCallError:
        before = len(script.requests)
        with pytest.raises(ProviderCallError) as raised:
            embedder.embed(texts)
        raised.value.sent = len(script.requests) - before  # type: ignore[attr-defined]
        return raised.value

    remote = make()

    # Four throttles: typed, still an IndexError, every attempt audited with its backoff.
    usage = EmbeddingUsage()
    script.steps = [429] * 4
    before = len(records(store))
    with usage_scope(usage):
        error = fails(remote, ["throttled"])
    assert isinstance(error, ProviderThrottledError) and isinstance(error, PalaceError)
    assert str(error) == "openrouter returned 429 for a 1-input batch"
    assert (error.kind, error.delivery, error.attempts, error.stop, error.code) == (
        "throttled",
        "rejected",
        4,
        "attempts-exhausted",
        "429",
    )
    added = records(store)[before:]
    assert [r["attempt"] for r in added] == [1, 2, 3, 4]
    assert len(sleeps) == 3 and all(0.5 * 2**n <= d <= 2**n for n, d in enumerate(sleeps))
    assert [r["backoff_ms"] for r in added] == [0] + [round(d * 1000) for d in sleeps]
    summary = usage.summary()
    assert (summary.requests, summary.failed_requests, summary.throttled_requests) == (4, 4, 4)

    # Each failure's kind and delivery; definite refusals stop at once.
    table: list[tuple[list[Step], str, str, int, str]] = [
        ([400], "rejected", "rejected", 1, "not-retryable"),
        ([503] * 4, "unavailable", "rejected", 4, "attempts-exhausted"),
        ([500] * 4, "unavailable", "unknown", 4, "attempts-exhausted"),
        ([504] * 4, "timeout", "unknown", 4, "attempts-exhausted"),
        ([httpx.ConnectError] * 4, "connection", "not-sent", 4, "attempts-exhausted"),
        ([httpx.ConnectTimeout] * 4, "timeout", "not-sent", 4, "attempts-exhausted"),
        ([httpx.ReadTimeout] * 4, "timeout", "unknown", 4, "attempts-exhausted"),
        ([httpx.RemoteProtocolError] * 4, "connection", "unknown", 4, "attempts-exhausted"),
    ]
    for steps, kind, delivery, attempts, stop in table:
        script.steps = list(steps)
        error = fails(remote, ["classified"])
        assert type(error) is ProviderCallError
        assert (error.kind, error.delivery, error.attempts, error.stop) == (
            kind,
            delivery,
            attempts,
            stop,
        ), steps
        assert error.sent == attempts  # type: ignore[attr-defined]

    # Earlier uncertainty survives a later throttle.
    usage = EmbeddingUsage()
    script.steps = [httpx.ReadTimeout, 429, 429, 429]
    with usage_scope(usage):
        error = fails(remote, ["timeout then throttle"])
    assert isinstance(error, ProviderThrottledError)
    assert (error.kind, error.delivery) == ("throttled", "unknown")
    assert (usage.summary().throttled_requests, usage.summary().indeterminate_requests) == (3, 1)

    # Retry-After: beyond the cap ends at once with the uncapped hint; a short one is a
    # floor that jitter only raises; an HTTP-date is read against Date; garbage is ignored.
    before_sleeps = len(sleeps)
    script.steps = [(429, {"Retry-After": "120"}, {})]
    error = fails(remote, ["long hint"])
    assert (error.retry_after, error.stop, error.attempts) == (120.0, "hint-beyond-cap", 1)
    assert len(sleeps) == before_sleeps
    date_hint = {
        "Retry-After": "Wed, 18 Nov 2026 00:00:10 GMT",
        "Date": "Wed, 18 Nov 2026 00:00:00 GMT",
    }
    for rng, headers, expected in (
        (1.0, {"Retry-After": "2"}, 3.0),
        (0.0, date_hint, 10.0),
        (0.0, {"Retry-After": "soon"}, 1.0),
    ):
        script.steps = [(429, headers, {})]
        hinted = make(_rng=lambda value=rng: value)
        assert hinted.embed(["hinted"]) == [[0.0, 1.0, 2.0, 3.0]]
        assert sleeps[-1] == expected
        hinted.close()

    # An HTTP-date hint without a readable Date is measured against the injected UTC clock:
    # beyond the cap it stops at once with the hint; within it, no retry before the hint.
    fixed_now = datetime(2026, 11, 18, 0, 0, 0, tzinfo=UTC)
    dated = make(_now=lambda: fixed_now)
    for extra in ({}, {"Date": "not a date"}):
        before_sleeps = len(sleeps)
        script.steps = [(429, {"Retry-After": "Wed, 18 Nov 2026 00:01:30 GMT", **extra}, {})]
        error = fails(dated, ["dated hint"])
        assert (error.retry_after, error.stop, error.attempts) == (90.0, "hint-beyond-cap", 1)
        assert len(sleeps) == before_sleeps
        script.steps = [(429, {"Retry-After": "Wed, 18 Nov 2026 00:00:20 GMT", **extra}, {})]
        before = len(script.requests)
        assert dated.embed(["dated hint"]) == [[0.0, 1.0, 2.0, 3.0]]
        assert len(script.requests) - before == 2 and 20.0 <= sleeps[-1] <= 30.0
    dated.close()

    # Each request's timeout is bounded by the scope's remaining time, per request; a passed
    # deadline sends nothing; a hint the deadline cannot accommodate stops without sleeping.
    with call_scope(deadline=time.monotonic() + 5):
        remote.embed(["bounded"])
    bound = script.requests[-1].extensions["timeout"]
    assert all(4 < bound[phase] <= 5 for phase in ("connect", "read", "write", "pool"))
    remote.embed(["default"])
    assert script.requests[-1].extensions["timeout"]["read"] == 120.0
    with call_scope(deadline=time.monotonic() - 1):
        error = fails(remote, ["late"])
    assert (error.kind, error.delivery, error.attempts) == ("deadline", "not-sent", 0)
    assert error.sent == 0  # type: ignore[attr-defined]
    before_sleeps = len(sleeps)
    script.steps = [(429, {"Retry-After": "10"}, {})]
    with call_scope(deadline=time.monotonic() + 5):
        error = fails(remote, ["deadline hint"])
    assert (error.stop, error.retry_after, len(sleeps)) == ("beyond-deadline", 10.0, before_sleeps)

    # The pacer sees one acquire per physical attempt with its byte size and one settle each.
    pacer = RecordingPacer()
    paced = make(pacer=pacer, purpose="query", max_batch=2)
    texts = ["漢字🙂" * 5, "é" * 30, "̈́" * 20]
    script.steps = ["ok", 429]
    assert len(paced.embed(texts)) == 3
    assert [(r.attempt, r.inputs, r.request_bytes) for r in pacer.acquired] == [
        (1, 2, sum(map(request_units, texts[:2]))),
        (1, 1, request_units(texts[2])),
        (2, 1, request_units(texts[2])),
    ]
    assert request_units(texts[2]) != len(texts[2])
    assert {(r.provider, r.model, r.operation, r.purpose) for r in pacer.acquired} == {
        ("openrouter", "Other/Model", "embeddings", "query")
    }
    assert pacer.settled == [("ok", None), ("throttled", None), ("ok", None)]
    assert not pacer.open

    # Partial success: the answered leading inputs are counted, not returned.
    script.steps = ["ok", 400]
    error = fails(paced, texts)
    assert (error.completed_inputs, error.kind, error.attempts) == (2, "rejected", 1)

    # Diagnostics are library-owned: provider text (an echoed input, a credential, a huge
    # multiline body, a raw non-JSON body), transport error text and a claimed upstream
    # identity reach neither the error, its fields nor the audit.
    input_marker = "CORPUSMARKER-input-never-echoed"
    secret = "SYNTHETICOPENROUTERKEYFIXTURE"
    hostile = f"{input_marker}\nkey {secret}\n" + "line of provider text\n" * 5000
    markers = (input_marker, secret, "provider text")
    before_audit = len(records(store))
    hostile_cases: list[tuple[list[Step], str]] = [
        ([(400, {}, {"error": {"message": hostile}})], "returned 400 for a 1-input batch"),
        ([(429, {}, {"error": {"message": hostile}})] * 4, "returned 429 for a 1-input batch"),
        ([(400, {}, hostile)], "returned 400 for a 1-input batch"),
        ([httpx.ConnectError(hostile)] * 4, "request failed: ConnectError"),
        ([httpx.ReadTimeout(hostile)] * 4, "request failed: ReadTimeout"),
    ]
    for steps, shown in hostile_cases:
        script.steps = list(steps)
        error = fails(remote, [input_marker])
        assert str(error) == f"openrouter {shown}"
        fields = repr((error.code, error.provider, error.operation, error.history, error.args))
        assert not any(marker in fields for marker in markers)
    written = json.dumps(records(store)[before_audit:])
    assert not any(marker in written for marker in markers)

    # Unusable answers are processed and never retried. A diverging upstream is never named
    # in the error; the audit names it only in a short known shape and otherwise withholds it.
    claimed: list[object] = []

    def divergent(request: httpx.Request) -> httpx.Response:
        data = [{"index": 0, "embedding": [0.0] * DIM}]
        return httpx.Response(200, json={"data": data, "provider": claimed[-1]})

    other = OpenRouterEmbedder(
        store=store,
        model="Other/Model",
        upstream=UPSTREAM,
        api_key="synthetic",
        dim=DIM,
        transport=httpx.MockTransport(divergent),
    )
    for identity, audited in (("Other", "Other"), (hostile, None), (None, None)):
        claimed.append(identity)
        before_audit = len(records(store))
        with pytest.raises(ProviderCallError, match="upstream divergence") as raised:
            other.embed(["divergent"])
        assert str(raised.value) == (
            f"openrouter upstream divergence: configured='{UPSTREAM}' was not the upstream "
            "that answered — build aborted"
        )
        assert (raised.value.kind, raised.value.delivery, raised.value.attempts) == (
            "response-invalid",
            "processed",
            1,
        )
        (record,) = records(store)[before_audit:]
        assert record["upstream_observed"] == audited
        assert record.get("upstream_observed_withheld", False) is (identity == hostile)
        assert not any(marker in json.dumps(record) for marker in markers)
    other.close()
    script.steps = [429, 429]
    before = len(script.requests)
    with pytest.raises(ProviderThrottledError):
        remote.probe()
    assert len(script.requests) - before == 1
    remote.close()
    paced.close()
