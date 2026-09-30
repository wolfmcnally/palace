"""Audited operator probe for OpenRouter's embedding endpoint."""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any, NoReturn

import httpx

import palace.index.embedder as embedder_module
from palace.daemons.capture.config import BOISE_TZ
from palace.index._errors import IndexError
from palace.index.config import EMBED_DIM, EMBEDDER_MODEL, OPENROUTER_BASE_URL
from palace.index.egress import append_record, build_record, cloud_egress_path
from palace.index.embedder import OllamaEmbedder

__all__ = [
    "capture_fixtures",
    "main",
    "probe_endpoints",
    "probe_concurrency",
    "probe_pin",
    "probe_throughput",
    "run_sustained",
]


REMOTE_MODEL = "Qwen/Qwen3-Embedding-8B"
PINNED_UPSTREAM = "DeepInfra"
DIFFERENT_UPSTREAM = "Nebius"
NONEXISTENT_UPSTREAM = "Palace-Nonexistent-Upstream"
FIXTURE_TEXT = "Palace embedding venue identity fixture, captured for Phase 3.3."
RECORDED_COST_USD_PER_MILLION_TOKENS = 0.01
COST_RESERVE_MULTIPLIER = 10.0
DEFAULT_INPUT_CHARS = 1400
DEFAULT_REQUEST_INPUTS = 20


@dataclass(frozen=True, slots=True)
class SustainedResult:
    """Bounded sustained-probe outcome, suitable for a durable report."""

    reason: str
    duration_seconds: float
    concurrency: int
    requests: int
    input_tokens: float
    cost_usd: float
    max_cost_usd: float
    reserve_per_request_usd: float
    status_counts: dict[int, int]
    engine_overloaded_count: int
    retry_after_values: tuple[str, ...]
    quarter_tokens_per_second: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class _SustainedAttempt:
    request_number: int
    status: int
    elapsed: float
    tokens: float
    cost_usd: float
    retry_after: str | None
    engine_overloaded: bool


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise IndexError(message)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument("--store", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--pin", action="store_true")
    modes.add_argument("--throughput", action="store_true")
    modes.add_argument("--capture-fixtures", type=Path)
    modes.add_argument("--sustained", action="store_true")
    parser.add_argument(
        "--concurrency",
        nargs="?",
        const="curve",
        metavar="N",
        help="run the concurrency curve, or set N with --sustained",
    )
    parser.add_argument("--expect-pin-honored", action="store_true")
    parser.add_argument("--expect-upstreams")
    parser.add_argument("--batch-sizes", default="1,5,10,20,40")
    parser.add_argument("--levels", default="1,2,4,8,16")
    parser.add_argument("--input-chars", type=int, default=DEFAULT_INPUT_CHARS)
    parser.add_argument("--request-inputs", type=int, default=DEFAULT_REQUEST_INPUTS)
    parser.add_argument("--minutes", type=float)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    curve_mode = args.concurrency == "curve"
    named_modes = sum(
        bool(value) for value in (args.pin, args.throughput, args.capture_fixtures, args.sustained)
    )
    if named_modes + int(curve_mode) != 1:
        parser.error(
            "select exactly one mode: --pin, --throughput, --concurrency, "
            "--sustained, or --capture-fixtures"
        )
    if args.input_chars < 1 or args.request_inputs < 1:
        parser.error("--input-chars and --request-inputs must be positive")
    args.concurrency_level = None
    if args.sustained:
        if args.concurrency in (None, "curve"):
            parser.error("--sustained requires --concurrency N")
        try:
            args.concurrency_level = int(args.concurrency)
        except ValueError as exc:
            raise IndexError("--concurrency N must be an integer") from exc
        if args.concurrency_level < 1:
            parser.error("--concurrency N must be positive")
        if args.minutes is None or args.minutes <= 0:
            parser.error("--sustained requires positive --minutes M")
        if args.max_cost_usd is None:
            parser.error("--sustained requires --max-cost-usd X")
        if args.max_cost_usd <= 0:
            parser.error("--max-cost-usd must be positive")
        if args.report is None:
            parser.error("--sustained requires --report PATH")
    elif args.concurrency not in (None, "curve"):
        parser.error("--concurrency N is valid only with --sustained")
    return args


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _refuse_store_under_source_root(store: Path) -> None:
    resolved = store.expanduser().resolve()
    try:
        resolved.relative_to(_source_root())
    except ValueError:
        return
    raise IndexError(f"probe --store must be outside the palace source root: {resolved}")


def _audit_count(store: Path) -> int:
    audit_dir = store / "events" / "cloud-egress"
    if not audit_dir.exists():
        return 0
    return sum(
        len(file_path.read_text(encoding="utf-8").splitlines())
        for file_path in sorted(audit_dir.glob("*.jsonl"))
    )


def _client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return embedder_module._build_client(timeout=120.0, transport=transport)


def _record_attempt(
    *,
    store: Path,
    kind: str,
    upstream_configured: str | None,
    upstream_observed: str | None,
    texts: Sequence[str],
    response: httpx.Response | None,
    body: dict[str, Any] | None,
    latency_ms: int,
) -> None:
    usage = body.get("usage") if body is not None else None
    prompt_tokens: int | None = None
    cost_usd: float | None = None
    if response is not None and response.status_code < 400 and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        cost = usage.get("cost")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            prompt_tokens = prompt
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_usd = float(cost)
    append_record(
        store=store,
        record=build_record(
            kind=kind,
            provider="openrouter",
            upstream_configured=upstream_configured,
            upstream_observed=upstream_observed,
            model=REMOTE_MODEL,
            dim=EMBED_DIM,
            texts=texts,
            prompt_tokens=prompt_tokens,
            cost_usd=cost_usd,
            http_status=response.status_code if response is not None else None,
            latency_ms=latency_ms,
        ),
    )


def _decode_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        decoded = response.json()
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _embedding_attempt(
    *,
    client: httpx.Client,
    store: Path,
    api_key: str,
    upstream: str,
    texts: Sequence[str],
) -> tuple[httpx.Response, dict[str, Any] | None, float]:
    started = time.monotonic()
    response: httpx.Response | None = None
    body: dict[str, Any] | None = None
    request_error: httpx.RequestError | None = None
    try:
        response = client.post(
            f"{OPENROUTER_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": REMOTE_MODEL,
                "input": list(texts),
                "provider": {
                    "order": [upstream],
                    "allow_fallbacks": False,
                    "zdr": True,
                    "data_collection": "deny",
                },
            },
        )
        body = _decode_body(response)
    except httpx.RequestError as exc:
        request_error = exc
    elapsed = time.monotonic() - started
    observed = body.get("provider") if body is not None else None
    _record_attempt(
        store=store,
        kind="cloud_embed_call",
        upstream_configured=upstream,
        upstream_observed=observed if isinstance(observed, str) else None,
        texts=texts,
        response=response,
        body=body,
        latency_ms=max(0, round(elapsed * 1000)),
    )
    if request_error is not None:
        raise IndexError(f"openrouter request failed: {request_error}") from request_error
    assert response is not None
    return response, body, elapsed


def probe_endpoints(*, client: httpx.Client, store: Path, api_key: str) -> list[str]:
    """List endpoint names and audit the metadata request."""
    started = time.monotonic()
    response: httpx.Response | None = None
    body: dict[str, Any] | None = None
    request_error: httpx.RequestError | None = None
    try:
        response = client.get(
            f"{OPENROUTER_BASE_URL}/models/{REMOTE_MODEL}/endpoints",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        body = _decode_body(response)
    except httpx.RequestError as exc:
        request_error = exc
    elapsed = time.monotonic() - started
    _record_attempt(
        store=store,
        kind="cloud_metadata_call",
        upstream_configured=None,
        upstream_observed=None,
        texts=(),
        response=response,
        body=body,
        latency_ms=max(0, round(elapsed * 1000)),
    )
    if request_error is not None:
        raise IndexError(f"openrouter endpoint listing failed: {request_error}") from request_error
    assert response is not None
    if response.status_code >= 400:
        raise IndexError(f"openrouter endpoint listing returned {response.status_code}")
    endpoints: Any = body.get("data") if body is not None else None
    if isinstance(endpoints, dict):
        endpoints = endpoints.get("endpoints")
    names: list[str] = []
    if isinstance(endpoints, list):
        for item in endpoints:
            if not isinstance(item, dict):
                continue
            name = item.get("provider_name") or item.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def probe_pin(
    *,
    client: httpx.Client,
    store: Path,
    api_key: str,
    expect_pin_honored: bool,
    expected_upstreams: set[str] | None,
) -> None:
    endpoints = probe_endpoints(client=client, store=store, api_key=api_key)
    observed: dict[str, str | None] = {}
    for upstream in (PINNED_UPSTREAM, DIFFERENT_UPSTREAM, NONEXISTENT_UPSTREAM):
        response, body, _elapsed = _embedding_attempt(
            client=client,
            store=store,
            api_key=api_key,
            upstream=upstream,
            texts=(FIXTURE_TEXT,),
        )
        provider = body.get("provider") if body is not None else None
        observed[upstream] = provider if isinstance(provider, str) else None
        if upstream == NONEXISTENT_UPSTREAM:
            if response.status_code != 404:
                raise IndexError(
                    f"non-existent upstream returned {response.status_code}; expected 404"
                )
        elif response.status_code >= 400:
            raise IndexError(f"pinned {upstream} returned HTTP {response.status_code}")
    pin_honored = (
        observed[PINNED_UPSTREAM] == PINNED_UPSTREAM
        and observed[DIFFERENT_UPSTREAM] == DIFFERENT_UPSTREAM
    )
    if expect_pin_honored and not pin_honored:
        raise IndexError(f"provider pin was not honored: {observed}")
    if expected_upstreams is not None and not expected_upstreams.issubset(set(endpoints)):
        missing = sorted(expected_upstreams - set(endpoints))
        raise IndexError(f"endpoint listing missing expected upstreams: {', '.join(missing)}")
    print(f"pin_honored: {str(pin_honored).lower()}")
    print("observed: " + json.dumps(observed, sort_keys=True))
    print("upstreams: " + ", ".join(endpoints))


def _vectors(body: dict[str, Any] | None, expected: int) -> list[list[float]]:
    data = body.get("data") if body is not None else None
    if not isinstance(data, list) or len(data) != expected:
        actual = len(data) if isinstance(data, list) else 0
        raise IndexError(f"remote fixture returned {actual} vectors")
    ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
    vectors: list[list[float]] = []
    for item in ordered:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or len(vector) != EMBED_DIM:
            raise IndexError("remote fixture vector has wrong dimension")
        vectors.append([float(value) for value in vector])
    return vectors


def _input_text(*, input_chars: int, label: str) -> str:
    seed = f"{FIXTURE_TEXT} {label} "
    return (seed * ((input_chars // len(seed)) + 1))[:input_chars]


def _texts(*, count: int, input_chars: int, label: str) -> tuple[str, ...]:
    return tuple(
        _input_text(input_chars=input_chars, label=f"{label} item={index}")
        for index in range(count)
    )


def probe_throughput(
    *,
    client: httpx.Client,
    store: Path,
    api_key: str,
    batch_sizes: Sequence[int],
    input_chars: int = DEFAULT_INPUT_CHARS,
) -> None:
    """Measure prose-length serial batches after one discarded warm-up."""
    warmup_size = batch_sizes[0]
    warmup, _body, warmup_elapsed = _embedding_attempt(
        client=client,
        store=store,
        api_key=api_key,
        upstream=PINNED_UPSTREAM,
        texts=_texts(count=warmup_size, input_chars=input_chars, label="warmup"),
    )
    if warmup.status_code >= 400:
        raise IndexError(f"throughput warm-up returned HTTP {warmup.status_code}")
    print(f"discarded_cold_seconds: {warmup_elapsed:.3f}")
    print("| batch | seconds | inputs/s | approx tokens/s | HTTP | rate-limit remaining |")
    print("|---:|---:|---:|---:|---:|---|")
    points: list[tuple[int, float]] = []
    for batch_size in batch_sizes:
        response, _body, elapsed = _embedding_attempt(
            client=client,
            store=store,
            api_key=api_key,
            upstream=PINNED_UPSTREAM,
            texts=_texts(count=batch_size, input_chars=input_chars, label=f"batch={batch_size}"),
        )
        if response.status_code >= 400:
            raise IndexError(f"throughput batch {batch_size} returned HTTP {response.status_code}")
        remaining = response.headers.get("x-ratelimit-remaining", "not returned")
        rate = batch_size / elapsed if elapsed > 0 else math.inf
        tokens_per_second = (batch_size * input_chars / 4) / elapsed if elapsed > 0 else math.inf
        points.append((batch_size, elapsed))
        print(
            f"| {batch_size} | {elapsed:.3f} | {rate:.2f} | {tokens_per_second:.1f} | "
            f"{response.status_code} | {remaining} |"
        )
    print(f"fitted_per_request_overhead_seconds: {_linear_intercept(points):.6f}")


def _linear_intercept(points: Sequence[tuple[int, float]]) -> float:
    if len(points) < 2:
        return points[0][1] if points else 0.0
    x_mean = sum(x for x, _y in points) / len(points)
    y_mean = sum(y for _x, y in points) / len(points)
    denominator = sum((x - x_mean) ** 2 for x, _y in points)
    if denominator == 0:
        return max(0.0, y_mean)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    return max(0.0, y_mean - slope * x_mean)


def probe_concurrency(
    *,
    client: httpx.Client,
    store: Path,
    api_key: str,
    levels: Sequence[int],
    input_chars: int = DEFAULT_INPUT_CHARS,
    request_inputs: int = DEFAULT_REQUEST_INPUTS,
) -> None:
    """Measure a start-barrier concurrency curve with per-level baselines."""
    warmup, _body, cold_elapsed = _embedding_attempt(
        client=client,
        store=store,
        api_key=api_key,
        upstream=PINNED_UPSTREAM,
        texts=_texts(count=request_inputs, input_chars=input_chars, label="concurrency-warmup"),
    )
    if warmup.status_code >= 400:
        raise IndexError(f"concurrency warm-up returned HTTP {warmup.status_code}")
    print(f"discarded_cold_seconds: {cold_elapsed:.3f}")
    print(
        "| N | wall seconds | approx tokens/s | request mean | send stagger | "
        "receive fan-out | speedup | efficiency |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for level in levels:
        baseline_response, _baseline_body, serial_elapsed = _embedding_attempt(
            client=client,
            store=store,
            api_key=api_key,
            upstream=PINNED_UPSTREAM,
            texts=_texts(
                count=request_inputs,
                input_chars=input_chars,
                label=f"n={level}-baseline",
            ),
        )
        if baseline_response.status_code >= 400:
            raise IndexError(
                f"concurrency N={level} baseline returned HTTP {baseline_response.status_code}"
            )
        barrier = Barrier(level + 1)

        def worker(
            worker_index: int,
            *,
            level_value: int = level,
            barrier_value: Barrier = barrier,
        ) -> tuple[float, float, float, int]:
            texts = _texts(
                count=request_inputs,
                input_chars=input_chars,
                label=f"n={level_value}-worker={worker_index}",
            )
            barrier_value.wait()
            sent = time.monotonic()
            response, _body, elapsed = _embedding_attempt(
                client=client,
                store=store,
                api_key=api_key,
                upstream=PINNED_UPSTREAM,
                texts=texts,
            )
            received = time.monotonic()
            return sent, received, elapsed, response.status_code

        with ThreadPoolExecutor(max_workers=level) as executor:
            futures = [executor.submit(worker, index) for index in range(level)]
            barrier.wait()
            results = [future.result() for future in futures]
        bad_statuses = [status for _sent, _received, _elapsed, status in results if status >= 400]
        if bad_statuses:
            raise IndexError(f"concurrency N={level} returned HTTP {bad_statuses[0]}")
        sends = [sent for sent, _received, _elapsed, _status in results]
        receives = [received for _sent, received, _elapsed, _status in results]
        elapsed_values = [elapsed for _sent, _received, elapsed, _status in results]
        wall = max(receives) - min(sends)
        send_stagger = max(sends) - min(sends)
        receive_fanout = max(receives) - min(receives)
        speedup = level * serial_elapsed / wall if wall > 0 else math.inf
        efficiency = speedup / level
        tokens_per_second = (
            level * request_inputs * input_chars / 4 / wall if wall > 0 else math.inf
        )
        print(
            f"| {level} | {wall:.3f} | {tokens_per_second:.1f} | "
            f"{statistics_mean(elapsed_values):.3f} | {send_stagger:.6f} | "
            f"{receive_fanout:.3f} | {speedup:.3f} | {efficiency:.3f} |"
        )


def run_sustained(
    *,
    client: httpx.Client,
    store: Path,
    api_key: str,
    duration_seconds: float,
    concurrency: int,
    max_cost_usd: float,
    report: Path,
    input_chars: int = DEFAULT_INPUT_CHARS,
    request_inputs: int = DEFAULT_REQUEST_INPUTS,
) -> SustainedResult:
    """Run a capped sustained measurement, reserving cost before dispatch."""
    reserve = _reserve_for_request(input_chars=input_chars, input_count=request_inputs)
    cost_cap = Decimal(str(max_cost_usd))
    started = time.monotonic()
    deadline = started + duration_seconds
    spent = Decimal("0")
    reserved = Decimal("0")
    next_request = 1
    completed: list[tuple[float, _SustainedAttempt]] = []
    status_counts: dict[int, int] = {}
    engine_overloaded_count = 0
    retry_after_values: list[str] = []
    cap_reached = False

    def attempt(request_number: int) -> _SustainedAttempt:
        response, body, elapsed = _embedding_attempt(
            client=client,
            store=store,
            api_key=api_key,
            upstream=PINNED_UPSTREAM,
            texts=_texts(
                count=request_inputs,
                input_chars=input_chars,
                label=f"sustained-request={request_number}",
            ),
        )
        retry_after = response.headers.get("retry-after")
        if response.status_code < 400:
            observed = body.get("provider") if body is not None else None
            if observed != PINNED_UPSTREAM:
                found = observed if isinstance(observed, str) else "none"
                raise IndexError(
                    f"sustained request {request_number} upstream divergence: observed={found}"
                )
            usage = body.get("usage") if body is not None else None
            cost = usage.get("cost") if isinstance(usage, dict) else None
            if not isinstance(cost, (int, float)) or isinstance(cost, bool):
                raise IndexError(f"sustained request {request_number} succeeded without usage.cost")
            cost_value = float(cost)
        else:
            cost_value = 0.0
        error_value = body.get("error") if body is not None else None
        return _SustainedAttempt(
            request_number=request_number,
            status=response.status_code,
            elapsed=elapsed,
            tokens=request_inputs * input_chars / 4,
            cost_usd=cost_value,
            retry_after=retry_after,
            engine_overloaded="engine_overloaded" in json.dumps(error_value).lower(),
        )

    inflight: dict[Future[_SustainedAttempt], Decimal] = {}
    error: BaseException | None = None
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while True:
            now = time.monotonic()
            while not cap_reached and now < deadline and len(inflight) < concurrency:
                if spent + reserved + reserve > cost_cap:
                    cap_reached = True
                    break
                future = executor.submit(attempt, next_request)
                inflight[future] = reserve
                reserved += reserve
                next_request += 1
                now = time.monotonic()
            if not inflight:
                break
            done, _pending = wait(inflight, return_when=FIRST_COMPLETED)
            for future in done:
                held = inflight.pop(future)
                reserved -= held
                try:
                    attempt_result = future.result()
                except BaseException as exc:
                    error = exc
                    cap_reached = True
                    continue
                spent += Decimal(str(attempt_result.cost_usd))
                completed.append((time.monotonic(), attempt_result))
                status_counts[attempt_result.status] = (
                    status_counts.get(attempt_result.status, 0) + 1
                )
                if attempt_result.engine_overloaded:
                    engine_overloaded_count += 1
                if attempt_result.retry_after is not None:
                    retry_after_values.append(attempt_result.retry_after)
            if error is not None:
                for future in inflight:
                    future.cancel()
                break
            if cap_reached and not inflight:
                break
            if time.monotonic() >= deadline and not inflight:
                break
        if error is not None:
            for future in tuple(inflight):
                with suppress(BaseException):
                    future.result()
            raise error

    finished = time.monotonic()
    duration = max(0.0, finished - started)
    reason = "cost-cap" if cap_reached else "duration"
    summary = SustainedResult(
        reason=reason,
        duration_seconds=duration,
        concurrency=concurrency,
        requests=len(completed),
        input_tokens=sum(item.tokens for _stamp, item in completed),
        cost_usd=float(spent),
        max_cost_usd=max_cost_usd,
        reserve_per_request_usd=float(reserve),
        status_counts=status_counts,
        engine_overloaded_count=engine_overloaded_count,
        retry_after_values=tuple(retry_after_values),
        quarter_tokens_per_second=_quarter_rates(
            completed=completed, started=started, duration=max(duration, 1e-9)
        ),
    )
    _write_sustained_report(report, summary)
    return summary


def _reserve_for_request(*, input_chars: int, input_count: int) -> Decimal:
    estimated_tokens = Decimal(input_count * input_chars) / Decimal(4)
    base_cost = (
        estimated_tokens * Decimal(str(RECORDED_COST_USD_PER_MILLION_TOKENS)) / Decimal(1_000_000)
    )
    return base_cost * Decimal(str(COST_RESERVE_MULTIPLIER))


def _quarter_rates(
    *, completed: Sequence[tuple[float, _SustainedAttempt]], started: float, duration: float
) -> tuple[float, float, float, float]:
    tokens = [0.0, 0.0, 0.0, 0.0]
    for stamp, attempt in completed:
        fraction = min(0.999999, max(0.0, (stamp - started) / duration))
        tokens[int(fraction * 4)] += attempt.tokens
    quarter_seconds = duration / 4
    return cast_quarters(tuple(value / quarter_seconds for value in tokens))


def cast_quarters(values: tuple[float, ...]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise ValueError("quarter rate calculation must produce four values")
    return values[0], values[1], values[2], values[3]


def _write_sustained_report(report: Path, result: SustainedResult) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    statuses = ", ".join(f"{key}:{value}" for key, value in sorted(result.status_counts.items()))
    quarters = ", ".join(f"{value:.1f}" for value in result.quarter_tokens_per_second)
    report.write_text(
        "# Sustained remote embedding probe\n\n"
        f"- ended_by: {result.reason}\n"
        f"- duration_seconds: {result.duration_seconds:.3f}\n"
        f"- concurrency: {result.concurrency}\n"
        f"- requests: {result.requests}\n"
        f"- input_tokens_estimated: {result.input_tokens:.1f}\n"
        f"- effective_tokens_per_second: "
        f"{result.input_tokens / max(result.duration_seconds, 1e-9):.1f}\n"
        f"- cost_usd: {result.cost_usd:.8f}\n"
        f"- max_cost_usd: {result.max_cost_usd:.8f}\n"
        f"- reserve_per_request_usd: {result.reserve_per_request_usd:.8f}\n"
        f"- status_counts: {statuses}\n"
        f"- engine_overloaded_count: {result.engine_overloaded_count}\n"
        f"- retry_after: {', '.join(result.retry_after_values)}\n"
        f"- quarter_tokens_per_second: {quarters}\n",
        encoding="utf-8",
    )


def statistics_mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write_f32(file_path: Path, vector: Sequence[float]) -> None:
    file_path.write_bytes(struct.pack(f"<{len(vector)}f", *vector))


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return 1.0 - dot / (left_norm * right_norm)


def capture_fixtures(*, client: httpx.Client, store: Path, api_key: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    response_a, body_a, _ = _embedding_attempt(
        client=client,
        store=store,
        api_key=api_key,
        upstream=PINNED_UPSTREAM,
        texts=(FIXTURE_TEXT,),
    )
    response_b, body_b, _ = _embedding_attempt(
        client=client,
        store=store,
        api_key=api_key,
        upstream=PINNED_UPSTREAM,
        texts=(FIXTURE_TEXT,),
    )
    if response_a.status_code >= 400 or response_b.status_code >= 400:
        raise IndexError("remote fixture capture failed")
    remote = _vectors(body_a, 1)[0]
    remote_repeat = _vectors(body_b, 1)[0]
    local_embedder = OllamaEmbedder()
    try:
        local_embedder.probe()
        local = local_embedder.embed((FIXTURE_TEXT,))[0]
    finally:
        local_embedder.close()
    _write_f32(destination / "ollama.f32", local)
    _write_f32(destination / "openrouter.f32", remote)
    _write_f32(destination / "openrouter_repeat.f32", remote_repeat)
    venues = {
        "capture_date": datetime.now(BOISE_TZ).date().isoformat(),
        "text": FIXTURE_TEXT,
        "local_model": EMBEDDER_MODEL,
        "remote_model": REMOTE_MODEL,
        "pinned_upstream": PINNED_UPSTREAM,
        "local_vs_openrouter_one_minus_cos": _cosine_distance(local, remote),
        "openrouter_repeat_one_minus_cos": _cosine_distance(remote, remote_repeat),
    }
    (destination / "venues.json").write_text(
        json.dumps(venues, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        store = args.store.expanduser().resolve()
        _refuse_store_under_source_root(store)
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if api_key is None or not api_key.strip():
            raise IndexError("OPENROUTER_API_KEY is missing or empty")
        before = _audit_count(store)
        with _client() as client:
            if args.pin:
                expected = (
                    {item.strip() for item in args.expect_upstreams.split(",") if item.strip()}
                    if args.expect_upstreams
                    else None
                )
                probe_pin(
                    client=client,
                    store=store,
                    api_key=api_key,
                    expect_pin_honored=args.expect_pin_honored,
                    expected_upstreams=expected,
                )
            elif args.throughput:
                try:
                    batch_sizes = [int(item) for item in args.batch_sizes.split(",")]
                except ValueError as exc:
                    raise IndexError("--batch-sizes must be comma-separated integers") from exc
                if not batch_sizes or any(size <= 0 for size in batch_sizes):
                    raise IndexError("--batch-sizes values must be positive")
                probe_throughput(
                    client=client,
                    store=store,
                    api_key=api_key,
                    batch_sizes=batch_sizes,
                    input_chars=args.input_chars,
                )
            elif args.concurrency == "curve":
                try:
                    levels = [int(item) for item in args.levels.split(",")]
                except ValueError as exc:
                    raise IndexError("--levels must be comma-separated integers") from exc
                if not levels or any(level <= 0 for level in levels):
                    raise IndexError("--levels values must be positive")
                probe_concurrency(
                    client=client,
                    store=store,
                    api_key=api_key,
                    levels=levels,
                    input_chars=args.input_chars,
                    request_inputs=args.request_inputs,
                )
            elif args.sustained:
                result = run_sustained(
                    client=client,
                    store=store,
                    api_key=api_key,
                    duration_seconds=args.minutes * 60,
                    concurrency=args.concurrency_level,
                    max_cost_usd=args.max_cost_usd,
                    report=args.report,
                    input_chars=args.input_chars,
                    request_inputs=args.request_inputs,
                )
                print(f"sustained_ended_by: {result.reason}")
                print(f"sustained_requests: {result.requests}")
                print(f"sustained_cost_usd: {result.cost_usd:.8f}")
                print(f"sustained_report: {args.report}")
            else:
                capture_fixtures(
                    client=client,
                    store=store,
                    api_key=api_key,
                    destination=args.capture_fixtures,
                )
        after = _audit_count(store)
        print(f"audit_file: {cloud_egress_path(store, datetime.now(BOISE_TZ).date())}")
        print(f"audit_records_appended: {after - before}")
        return 0
    except (IndexError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
