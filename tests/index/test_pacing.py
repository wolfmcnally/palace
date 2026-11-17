"""Typed provider failures, the shared retry loop, call scopes and the reference pacer.

Every proof is hermetic: ``send`` callables stand in for providers, sleeps are recorded or
tiny, and no network is used.
"""

from __future__ import annotations

import contextvars
import functools
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import palace.index as index_api
from palace.index._errors import (
    DELIVERY_ORDER,
    IndexError,
    ProviderCallError,
    ProviderThrottledError,
    merge_delivery,
)
from palace.index.pacing import (
    AttemptFailure,
    AttemptInfo,
    IntervalPacer,
    PacedRequest,
    PacerRefused,
    PacerReservation,
    RetryPolicy,
    SettleOutcome,
    WaitInterrupted,
    call_scope,
    check_scope,
    current_scope,
    parse_retry_after,
    perform,
    remaining_seconds,
    scoped_lock,
    scoped_wait,
)

REQUEST = PacedRequest(
    provider="bedrock:test-region",
    model="test-model",
    operation="InvokeModel",
    purpose="document",
    inputs=2,
    request_bytes=40,
)


class FakeClock:
    """A monotonic clock that advances only when told to or when ``sleep`` is called."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def throttle(retry_after: float | None = None) -> AttemptFailure:
    return AttemptFailure(
        "bedrock InvokeModel failed in test-region: ThrottlingException: slow down",
        kind="throttled",
        delivery="rejected",
        code="ThrottlingException",
        retryable=True,
        retry_after=retry_after,
    )


def read_timeout() -> AttemptFailure:
    return AttemptFailure(
        "bedrock InvokeModel failed in test-region: ReadTimeoutError: read timed out",
        kind="timeout",
        delivery="unknown",
        code="ReadTimeoutError",
        retryable=True,
    )


def scripted(*outcomes: AttemptFailure | str) -> tuple[Callable[[AttemptInfo], str], list[int]]:
    """A ``send`` that raises or returns each outcome in turn and records attempt numbers."""
    calls: list[int] = []
    queue = list(outcomes)

    def send(info: AttemptInfo) -> str:
        calls.append(info.number)
        outcome = queue.pop(0)
        if isinstance(outcome, AttemptFailure):
            raise outcome
        return outcome

    return send, calls


class RecordingPacer:
    """A pacer that admits everything and records each acquire and settle."""

    def __init__(self, *, refuse: BaseException | None = None) -> None:
        self.acquired: list[PacedRequest] = []
        self.settled: list[tuple[object, SettleOutcome, float | None]] = []
        self.refuse = refuse
        self.settle_error: Exception | None = None

    def acquire(
        self,
        request: PacedRequest,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> object:
        if self.refuse is not None:
            raise self.refuse
        self.acquired.append(request)
        return ("reservation", len(self.acquired))

    def settle(
        self, reservation: object, outcome: SettleOutcome, *, retry_after: float | None
    ) -> None:
        self.settled.append((reservation, outcome, retry_after))
        if self.settle_error is not None:
            raise self.settle_error


def run(
    send: Callable[[AttemptInfo], str],
    *,
    clock: FakeClock,
    policy: RetryPolicy | None = None,
    pacer: Any = None,
    seed: int = 7,
    completed_inputs: int = 0,
) -> str:
    return perform(
        send,
        request=REQUEST,
        policy=policy if policy is not None else RetryPolicy(),
        pacer=pacer,
        completed_inputs=completed_inputs,
        rng=random.Random(seed).random,
        clock=clock,
        sleep=clock.sleep,
    )


# --------------------------------------------------------------------------- typed errors


def _typed_errors_are_index_errors_exported_and_merge_toward_evidence_of_work() -> None:
    assert issubclass(ProviderThrottledError, ProviderCallError)
    assert issubclass(ProviderCallError, IndexError)
    for name in (
        "ProviderCallError",
        "ProviderThrottledError",
        "RetryPolicy",
        "PacedRequest",
        "RequestPacer",
        "IntervalPacer",
        "call_scope",
    ):
        assert name in index_api.__all__ and hasattr(index_api, name)
    assert DELIVERY_ORDER == ("not-sent", "rejected", "unknown", "processed")
    assert merge_delivery("unknown", "rejected") == "unknown"
    assert merge_delivery("rejected", "unknown") == "unknown"
    assert merge_delivery("not-sent", "rejected") == "rejected"
    assert merge_delivery("processed", "unknown") == "processed"


def _four_throttles_raise_a_typed_rejected_throttle_with_the_adapter_message() -> None:
    clock = FakeClock()
    send, calls = scripted(throttle(), throttle(), throttle(), throttle())
    with pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock, completed_inputs=5)
    error = caught.value
    assert str(error) == "bedrock InvokeModel failed in test-region: ThrottlingException: slow down"
    assert calls == [1, 2, 3, 4] and error.attempts == 4 and len(clock.sleeps) == 3
    assert (error.kind, error.delivery, error.code, error.stop) == (
        "throttled",
        "rejected",
        "ThrottlingException",
        "attempts-exhausted",
    )
    assert error.provider == "bedrock:test-region" and error.operation == "InvokeModel"
    assert error.completed_inputs == 5 and error.throttled
    assert [item.kind for item in error.history] == ["throttled"] * 4
    copied = error.with_completed_inputs(9)
    assert type(copied) is ProviderThrottledError and str(copied) == str(error)
    assert copied.completed_inputs == 9 and copied.history == error.history


def _a_timeout_before_a_throttle_keeps_the_request_uncertain() -> None:
    clock = FakeClock()
    send, _ = scripted(read_timeout(), throttle(), throttle(), throttle())
    with pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock)
    assert caught.value.kind == "throttled" and caught.value.delivery == "unknown"
    assert [item.delivery for item in caught.value.history] == ["unknown"] + ["rejected"] * 3


def _a_timeout_then_a_pacer_refusal_keeps_the_uncertainty_and_sends_nothing_more() -> None:
    clock = FakeClock()

    class RefuseSecond(RecordingPacer):
        def acquire(self, request: PacedRequest, **kwargs: Any) -> object:
            if request.attempt == 2:
                raise PacerRefused("over budget")
            return super().acquire(request, **kwargs)

    pacer = RefuseSecond()
    send, calls = scripted(read_timeout(), "never")
    with pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock, pacer=pacer)
    error = caught.value
    assert type(error) is ProviderCallError and calls == [1]
    assert (error.kind, error.stop, error.delivery, error.attempts) == (
        "pacer-refused",
        "pacer-refused",
        "unknown",
        1,
    )
    assert isinstance(error.__cause__, PacerRefused)
    assert [outcome for _, outcome, _ in pacer.settled] == ["failed"]


def _a_non_retryable_refusal_stops_at_one_attempt_and_unexpected_errors_pass_through() -> None:
    clock = FakeClock()
    denied = AttemptFailure(
        "bedrock InvokeModel failed in test-region: AccessDeniedException: denied",
        kind="rejected",
        delivery="rejected",
        code="AccessDeniedException",
        retryable=False,
    )
    send, calls = scripted(denied)
    with pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock)
    assert calls == [1] and caught.value.stop == "not-retryable" and not caught.value.throttled
    assert clock.sleeps == []

    pacer = RecordingPacer()

    def broken(info: AttemptInfo) -> str:
        raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        run(broken, clock=clock, pacer=pacer)
    assert [outcome for _, outcome, _ in pacer.settled] == ["failed"]


def _attempt_failure_refuses_an_unusable_hint() -> None:
    for hint in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            throttle(hint)


# --------------------------------------------------------------------------- retry policy


def _jittered_delays_stay_inside_their_bounds_and_differ_between_seeds() -> None:
    policy = RetryPolicy(base_seconds=1.0, max_wait_seconds=30.0, jitter=0.5)
    rng = random.Random(1).random
    for attempt in range(1, 9):
        exponential = min(30.0, 2.0 ** (attempt - 1))
        for _ in range(50):
            delay = policy.delay(attempt, None, rng=rng)
            assert delay is not None and exponential * 0.5 <= delay <= exponential
    first = [policy.delay(1, None, rng=random.Random(1).random) for _ in range(1)]
    second = [policy.delay(1, None, rng=random.Random(2).random) for _ in range(1)]
    assert first != second
    assert RetryPolicy(jitter=0.0).delay(3, None, rng=rng) == 4.0


def _a_provider_hint_is_a_floor_never_lowered_and_a_long_hint_stops_retrying() -> None:
    policy = RetryPolicy(base_seconds=1.0, max_wait_seconds=30.0, jitter=0.5)
    rng = random.Random(3).random
    for _ in range(100):
        delay = policy.delay(1, 20.0, rng=rng)
        assert delay is not None and 20.0 <= delay <= 30.0
        assert policy.delay(1, 30.0, rng=rng) == 30.0
    assert policy.delay(1, 120.0, rng=rng) is None


def _a_hint_beyond_the_cap_ends_the_request_without_an_early_retry() -> None:
    clock = FakeClock()
    pacer = RecordingPacer()
    send, calls = scripted(throttle(120.0), "never")
    with pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock, pacer=pacer)
    error = caught.value
    assert calls == [1] and clock.sleeps == []
    assert error.stop == "hint-beyond-cap" and error.retry_after == 120.0
    assert error.retry_not_before == pytest.approx(clock.now + 120.0)
    assert pacer.settled[0][1:] == ("throttled", 120.0)


def _a_wait_that_would_cross_the_deadline_stops_now_with_the_provider_kind() -> None:
    clock = FakeClock()
    send, calls = scripted(throttle(20.0), "never")
    with call_scope(deadline=clock.now + 10.0), pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock)
    assert calls == [1] and clock.sleeps == []
    assert caught.value.stop == "beyond-deadline" and caught.value.retry_after == 20.0


def _the_retry_budget_bounds_the_whole_request() -> None:
    clock = FakeClock()
    policy = RetryPolicy(max_attempts=10, base_seconds=4.0, jitter=0.0, budget_seconds=10.0)
    send, calls = scripted(*([throttle()] * 10))
    with pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock, policy=policy)
    # 4 s then 8 s would cross the 10 s budget, so the second wait is never taken.
    assert calls == [1, 2] and clock.sleeps == [4.0] and caught.value.stop == "beyond-budget"


def _an_overslept_backoff_or_late_grant_never_starts_an_attempt_past_the_budget() -> None:
    policy = RetryPolicy(max_attempts=4, base_seconds=0.5, jitter=0.0, budget_seconds=1.0)
    clock = FakeClock()

    def oversleep(seconds: float) -> None:
        clock.sleep(seconds + 1.5)  # the 0.5 s backoff fits the budget but runs to 2.0 s

    send, calls = scripted(throttle(), "never")
    with pytest.raises(ProviderThrottledError) as caught:
        perform(
            send,
            request=REQUEST,
            policy=policy,
            rng=random.Random(1).random,
            clock=clock,
            sleep=oversleep,
        )
    assert calls == [1] and caught.value.stop == "beyond-budget"
    assert caught.value.code == "ThrottlingException" and caught.value.attempts == 1

    clock = FakeClock()

    class LateGrant(RecordingPacer):
        def acquire(self, request: PacedRequest, **kwargs: Any) -> object:
            if request.attempt == 2:
                clock.advance(2.0)  # a pacer may return after its deadline
            return super().acquire(request, **kwargs)

    pacer = LateGrant()
    send, calls = scripted(throttle(), "never")
    with pytest.raises(ProviderThrottledError) as caught:
        run(send, clock=clock, policy=policy, pacer=pacer)
    assert calls == [1] and caught.value.stop == "beyond-budget"
    assert [outcome for _, outcome, _ in pacer.settled] == ["throttled", "not-sent"]


def _retry_policy_validation() -> None:
    bad_policy: dict[str, Any]
    for bad_policy in (
        {"max_attempts": 0},
        {"base_seconds": -1.0},
        {"max_wait_seconds": float("inf")},
        {"jitter": 1.5},
        {"budget_seconds": 0.0},
    ):
        with pytest.raises(IndexError):
            RetryPolicy(**bad_policy)


def _no_pacer_no_scope_throttle_then_success_is_two_sends_and_one_jittered_sleep() -> None:
    clock = FakeClock()
    send, calls = scripted(throttle(), "vectors")
    assert run(send, clock=clock) == "vectors"
    assert calls == [1, 2] and len(clock.sleeps) == 1 and 0.5 <= clock.sleeps[0] <= 1.0


def _parse_retry_after_reads_seconds_and_dates_never_dropping_a_future_date() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after(" 12 ") == 12.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("-3") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("soon") is None
    date = "Tue, 17 Nov 2026 15:00:00 GMT"
    assert parse_retry_after("Tue, 17 Nov 2026 15:01:30 GMT", date=date) == 90.0
    assert parse_retry_after("Tue, 17 Nov 2026 14:59:00 GMT", date=date) == 0.0
    # Without a readable Date, a future HTTP-date is measured against the injected UTC clock,
    # never dropped; a naive clock or a zone-less date is read as UTC.
    clock_now = datetime(2026, 11, 17, 15, 0, 0, tzinfo=UTC)
    future = "Tue, 17 Nov 2026 15:01:30 GMT"
    assert parse_retry_after(future, now=lambda: clock_now) == 90.0
    assert parse_retry_after(future, date="garbage", now=lambda: clock_now) == 90.0
    assert parse_retry_after(future, date="", now=lambda: clock_now) == 90.0
    assert parse_retry_after(future, now=lambda: clock_now.replace(tzinfo=None)) == 90.0
    assert parse_retry_after("Tue, 17 Nov 2026 15:01:30 -0000", now=lambda: clock_now) == 90.0
    assert parse_retry_after(future, date=date, now=lambda: clock_now + timedelta(days=1)) == 90.0
    real = parse_retry_after("Fri, 01 Jan 2100 00:00:00 GMT")
    assert real is not None and real > 0


# --------------------------------------------------------------------------- pacer seam


def _the_pacer_sees_one_acquire_and_one_settle_per_physical_attempt() -> None:
    clock = FakeClock()
    pacer = RecordingPacer()
    send, calls = scripted(throttle(2.0), read_timeout(), "vectors")
    assert run(send, clock=clock, pacer=pacer) == "vectors"
    assert calls == [1, 2, 3]
    assert [request.attempt for request in pacer.acquired] == [1, 2, 3]
    assert all(
        (request.purpose, request.inputs, request.request_bytes) == ("document", 2, 40)
        for request in pacer.acquired
    )
    assert [(outcome, hint) for _, outcome, hint in pacer.settled] == [
        ("throttled", 2.0),
        ("failed", None),
        ("ok", None),
    ]
    assert [reservation for reservation, _, _ in pacer.settled] == [
        ("reservation", 1),
        ("reservation", 2),
        ("reservation", 3),
    ]


def _a_refusing_pacer_makes_no_request_and_reports_not_sent() -> None:
    clock = FakeClock()
    send, calls = scripted("never")
    with pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock, pacer=RecordingPacer(refuse=RuntimeError("closed")))
    assert calls == [] and caught.value.delivery == "not-sent" and caught.value.attempts == 0
    assert caught.value.kind == "pacer-refused" and caught.value.code == "RuntimeError"


def _a_settle_failure_never_discards_a_paid_answer_or_replaces_a_failure() -> None:
    clock = FakeClock()
    pacer = RecordingPacer()
    pacer.settle_error = RuntimeError("settle broke")
    send, _ = scripted("vectors")
    assert run(send, clock=clock, pacer=pacer) == "vectors"

    denied = AttemptFailure(
        "denied", kind="rejected", delivery="rejected", code="Denied", retryable=False
    )
    send, _ = scripted(denied)
    with pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock, pacer=pacer)
    assert caught.value.code == "Denied"
    assert caught.value.__notes__ == ["pacer settle failed: RuntimeError"]


def _send_receives_remaining_time_pacer_wait_and_previous_backoff() -> None:
    clock = FakeClock()
    seen: list[AttemptInfo] = []

    class SlowPacer(RecordingPacer):
        def acquire(self, request: PacedRequest, **kwargs: Any) -> object:
            clock.advance(0.25)
            return super().acquire(request, **kwargs)

    def send(info: AttemptInfo) -> str:
        seen.append(info)
        if info.number == 1:
            raise throttle(1.0)
        return "ok"

    policy = RetryPolicy(jitter=0.0)
    with call_scope(deadline=clock.now + 60.0):
        assert run(send, clock=clock, pacer=SlowPacer(), policy=policy) == "ok"
    assert [info.pacer_wait_seconds for info in seen] == [0.25, 0.25]
    assert [info.backoff_seconds for info in seen] == [0.0, 1.0]
    assert seen[0].remaining_seconds == pytest.approx(59.75)
    assert seen[1].remaining_seconds == pytest.approx(58.5)


# --------------------------------------------------------------------------- scopes


def _nested_scopes_take_the_earliest_deadline_and_keep_every_predicate() -> None:
    outer_flag = threading.Event()
    assert current_scope().deadline is None and remaining_seconds() is None
    with call_scope(deadline=100.0, cancelled=outer_flag.is_set):
        with call_scope(deadline=200.0) as inner:
            assert inner.deadline == 100.0 and len(inner.cancelled) == 1
            with call_scope() as default:
                assert default.deadline == 100.0
            outer_flag.set()
            with pytest.raises(WaitInterrupted) as caught:
                inner.check(50.0)
            assert caught.value.reason == "cancelled"
        with call_scope(deadline=50.0) as tighter:
            assert tighter.deadline == 50.0
    assert current_scope().deadline is None
    with pytest.raises(ValueError), call_scope(deadline=float("inf")):
        pass


def _a_raising_cancellation_predicate_is_a_distinct_interruption() -> None:
    def broken() -> bool:
        raise RuntimeError("predicate failed")

    with call_scope(cancelled=broken), pytest.raises(WaitInterrupted) as caught:
        check_scope()
    assert caught.value.reason == "cancellation-failed"
    assert isinstance(caught.value.__cause__, RuntimeError)


def _cancellation_during_a_backoff_stops_without_another_attempt() -> None:
    clock = FakeClock()
    flag = threading.Event()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if sum(clock.sleeps) >= 0.2:
            flag.set()

    send, calls = scripted(read_timeout(), "never")
    with call_scope(cancelled=flag.is_set), pytest.raises(ProviderCallError) as caught:
        perform(
            send,
            request=REQUEST,
            policy=RetryPolicy(jitter=0.0),
            rng=random.Random(1).random,
            clock=clock,
            sleep=sleep,
        )
    error = caught.value
    assert calls == [1] and error.attempts == 1
    assert (error.kind, error.stop, error.delivery) == ("cancelled", "interrupted", "unknown")
    assert error.history[-1].kind == "cancelled"
    assert sum(clock.sleeps) == pytest.approx(0.2) and max(clock.sleeps) <= 0.05 + 1e-9


def _an_overslept_backoff_past_the_deadline_starts_nothing_and_keeps_uncertainty() -> None:
    clock = FakeClock()
    pacer = RecordingPacer()

    def oversleep(seconds: float) -> None:
        clock.sleep(seconds + 5.0)  # a 1 s backoff inside a 3 s deadline that runs to 6 s

    send, calls = scripted(read_timeout(), "never")
    with call_scope(deadline=clock.now + 3.0), pytest.raises(ProviderCallError) as caught:
        perform(
            send,
            request=REQUEST,
            policy=RetryPolicy(jitter=0.0),
            pacer=pacer,
            rng=random.Random(1).random,
            clock=clock,
            sleep=oversleep,
        )
    error = caught.value
    assert calls == [1] and len(pacer.acquired) == 1
    assert (error.kind, error.stop, error.delivery, error.attempts) == (
        "deadline",
        "interrupted",
        "unknown",
        1,
    )

    # A pacer that grants after the deadline: its reservation is returned unsent.
    clock = FakeClock()

    class LateGrant(RecordingPacer):
        def acquire(self, request: PacedRequest, **kwargs: Any) -> object:
            if request.attempt == 2:
                clock.advance(10.0)
            return super().acquire(request, **kwargs)

    pacer = LateGrant()
    send, calls = scripted(read_timeout(), "never")
    with call_scope(deadline=clock.now + 3.0), pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock, pacer=pacer, policy=RetryPolicy(jitter=0.0))
    assert calls == [1] and caught.value.delivery == "unknown"
    assert caught.value.kind == "deadline"
    assert [outcome for _, outcome, _ in pacer.settled] == ["failed", "not-sent"]


def _an_expired_scope_starts_no_attempt() -> None:
    clock = FakeClock()
    send, calls = scripted("never")
    with call_scope(deadline=clock.now - 1), pytest.raises(ProviderCallError) as caught:
        run(send, clock=clock)
    assert calls == [] and caught.value.kind == "deadline" and caught.value.attempts == 0


def _scoped_wait_refuses_a_wait_it_cannot_finish() -> None:
    clock = FakeClock()
    with call_scope(deadline=clock.now + 1.0):
        with pytest.raises(WaitInterrupted) as caught:
            scoped_wait(2.0, clock=clock, sleep=clock.sleep)
        assert caught.value.reason == "deadline" and clock.sleeps == []
        scoped_wait(0.5, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [0.5]


def _scoped_lock_waits_no_longer_than_the_scope() -> None:
    lock = threading.Lock()
    lock.acquire()
    try:
        started = time.monotonic()
        with (
            call_scope(deadline=started + 0.1),
            pytest.raises(WaitInterrupted),
            scoped_lock(lock),
        ):
            pass
        assert time.monotonic() - started < 2.0
        flag = threading.Event()
        flag.set()
        with (
            call_scope(cancelled=flag.is_set),
            pytest.raises(WaitInterrupted) as caught,
            scoped_lock(lock),
        ):
            pass
        assert caught.value.reason == "cancelled"
    finally:
        lock.release()
    with scoped_lock(lock):
        assert lock.locked()
    assert not lock.locked()


def _a_copied_context_carries_the_scope_into_a_worker_thread() -> None:
    seen: list[float | None] = []
    with call_scope(deadline=123.0):
        context = contextvars.copy_context()
    worker = threading.Thread(
        target=context.run, args=(lambda: seen.append(current_scope().deadline),)
    )
    worker.start()
    worker.join()
    bare = threading.Thread(target=lambda: seen.append(current_scope().deadline))
    bare.start()
    bare.join()
    assert seen == [123.0, None]


# --------------------------------------------------------------------------- reference pacer


def request(purpose: str = "document", size: int = 10) -> PacedRequest:
    return PacedRequest(
        provider="test",
        model="m",
        operation="InvokeModel",
        purpose="query" if purpose == "query" else "document",
        inputs=1,
        request_bytes=size,
    )


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.001)


def queued(pacer: IntervalPacer, purpose: str) -> int:
    return len(pacer._queues["query" if purpose == "query" else "document"])


def queued_is(pacer: IntervalPacer, purpose: str, depth: int) -> bool:
    return queued(pacer, purpose) == depth


def length_is(items: list[str], expected: int) -> bool:
    return len(items) == expected


def _the_reference_pacer_never_exceeds_its_rate_across_many_threads(
    threads: list[threading.Thread],
) -> None:
    pacer = IntervalPacer(
        interval_seconds=0.1, requests_per_interval=3, bytes_per_interval=25, poll_seconds=0.002
    )
    grants: list[PacerReservation] = []
    lock = threading.Lock()

    def worker(purpose: str) -> None:
        for _ in range(3):
            reservation = pacer.acquire(request(purpose, 7), deadline=None, cancelled=None)
            with lock:
                grants.append(reservation)
            pacer.settle(reservation, "ok", retry_after=None)

    for index in range(6):
        thread = threading.Thread(target=worker, args=("query" if index % 2 else "document",))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(grants) == 18
    times = sorted(grant.granted_at for grant in grants)
    for start in times:
        window = [t for t in times if start <= t < start + 0.1]
        assert len(window) <= 3 and 7 * len(window) <= 25


def _a_queued_query_overtakes_documents_and_documents_keep_their_order(
    threads: list[threading.Thread],
) -> None:
    clock = FakeClock()
    pacer = IntervalPacer(
        interval_seconds=1.0, requests_per_interval=1, clock=clock, poll_seconds=0.001
    )
    pacer.settle(pacer.acquire(request(), deadline=None, cancelled=None), "ok", retry_after=None)
    order: list[str] = []

    def worker(name: str, purpose: str) -> None:
        reservation = pacer.acquire(request(purpose), deadline=None, cancelled=None)
        order.append(name)
        pacer.settle(reservation, "ok", retry_after=None)

    for name, purpose, depth in (("d1", "document", 1), ("d2", "document", 2), ("q1", "query", 1)):
        thread = threading.Thread(target=worker, args=(name, purpose))
        threads.append(thread)
        thread.start()
        wait_until(functools.partial(queued_is, pacer, purpose, depth))
    for expected in (1, 2, 3):
        clock.advance(1.0)
        wait_until(functools.partial(length_is, order, expected))
    assert order == ["q1", "d1", "d2"]


def _documents_progress_under_a_continuous_stream_of_queries(
    threads: list[threading.Thread],
) -> None:
    clock = FakeClock()
    pacer = IntervalPacer(
        interval_seconds=1.0,
        requests_per_interval=1,
        query_burst=2,
        clock=clock,
        poll_seconds=0.001,
    )
    pacer.settle(pacer.acquire(request(), deadline=None, cancelled=None), "ok", retry_after=None)
    order: list[str] = []
    lock = threading.Lock()

    def worker(name: str, purpose: str) -> None:
        reservation = pacer.acquire(request(purpose), deadline=None, cancelled=None)
        with lock:
            order.append(name)
        pacer.settle(reservation, "ok", retry_after=None)

    def start(name: str, purpose: str) -> None:
        thread = threading.Thread(target=worker, args=(name, purpose))
        threads.append(thread)
        thread.start()

    start("doc", "document")
    wait_until(lambda: queued(pacer, "document") == 1)
    for round_ in range(4):
        start(f"q{round_}", "query")
        wait_until(lambda: queued(pacer, "query") >= 1)
        clock.advance(1.0)
        wait_until(functools.partial(length_is, order, round_ + 1))
    # Two query grants while the document waited, then the document, despite queued queries.
    assert order[:3] == ["q0", "q1", "doc"]
    clock.advance(10.0)
    wait_until(lambda: len(order) == 5)


def _a_cancelled_queue_head_leaves_the_queue_and_the_next_waiter_proceeds(
    threads: list[threading.Thread],
) -> None:
    clock = FakeClock()
    pacer = IntervalPacer(
        interval_seconds=1.0, requests_per_interval=1, clock=clock, poll_seconds=0.001
    )
    pacer.settle(pacer.acquire(request(), deadline=None, cancelled=None), "ok", retry_after=None)
    cancel = threading.Event()
    outcomes: list[str] = []

    def head() -> None:
        try:
            pacer.acquire(request(), deadline=None, cancelled=cancel.is_set)
        except WaitInterrupted as signal:
            outcomes.append(signal.reason)

    def second() -> None:
        pacer.acquire(request(), deadline=None, cancelled=None)
        outcomes.append("granted")

    for target, depth in ((head, 1), (second, 2)):
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()
        wait_until(functools.partial(queued_is, pacer, "document", depth))
    cancel.set()
    wait_until(lambda: outcomes == ["cancelled"])
    assert queued(pacer, "document") == 1
    clock.advance(1.0)
    wait_until(lambda: outcomes == ["cancelled", "granted"])


def _a_pacer_wait_stops_at_the_deadline_inside_perform() -> None:
    clock = FakeClock()
    pacer = IntervalPacer(interval_seconds=60.0, requests_per_interval=1, poll_seconds=0.001)
    pacer.settle(pacer.acquire(request(), deadline=None, cancelled=None), "ok", retry_after=None)
    send, calls = scripted("never")
    started = time.monotonic()
    with call_scope(deadline=time.monotonic() + 0.05), pytest.raises(ProviderCallError) as caught:
        perform(send, request=REQUEST, policy=RetryPolicy(), pacer=pacer, sleep=clock.sleep)
    assert time.monotonic() - started < 2.0
    assert calls == [] and caught.value.kind == "deadline" and caught.value.stop == "interrupted"
    assert queued(pacer, "document") == 0


def _identical_requests_get_distinct_reservations_settled_exactly_once() -> None:
    pacer = IntervalPacer(interval_seconds=1.0, requests_per_interval=2)
    same = request()
    first = pacer.acquire(same, deadline=None, cancelled=None)
    second = pacer.acquire(same, deadline=None, cancelled=None)
    assert first is not second and first.request == second.request
    pacer.settle(first, "ok", retry_after=None)
    with pytest.raises(ValueError, match="already settled"):
        pacer.settle(first, "ok", retry_after=None)
    with pytest.raises(ValueError, match="not issued"):
        pacer.settle(object(), "ok", retry_after=None)
    other = IntervalPacer(interval_seconds=1.0, requests_per_interval=1)
    with pytest.raises(ValueError, match="not issued"):
        other.settle(second, "ok", retry_after=None)


def assert_held(pacer: IntervalPacer, clock: FakeClock, waiting: PacedRequest) -> None:
    """``waiting`` queues and is not admitted before its fake deadline passes."""
    outcome: list[str] = []

    def worker() -> None:
        try:
            pacer.settle(
                pacer.acquire(waiting, deadline=clock.now + 0.5, cancelled=None),
                "not-sent",
                retry_after=None,
            )
            outcome.append("granted")
        except WaitInterrupted as signal:
            outcome.append(signal.reason)

    thread = threading.Thread(target=worker)
    thread.start()
    purpose = waiting.purpose
    wait_until(lambda: queued(pacer, purpose) == 1 or bool(outcome))
    time.sleep(0.02)
    assert outcome == []
    clock.advance(0.5)
    thread.join(timeout=5)
    assert outcome == ["deadline"]


def _oversized_work_is_refused_and_not_sent_capacity_is_returned() -> None:
    clock = FakeClock()
    pacer = IntervalPacer(
        interval_seconds=10.0, bytes_per_interval=100, clock=clock, poll_seconds=0.001
    )
    with pytest.raises(PacerRefused):
        pacer.acquire(request(size=101), deadline=None, cancelled=None)
    held = pacer.acquire(request(size=60), deadline=None, cancelled=None)
    assert_held(pacer, clock, request(size=60))
    pacer.settle(held, "not-sent", retry_after=None)
    again = pacer.acquire(request(size=60), deadline=None, cancelled=None)
    pacer.settle(again, "failed", retry_after=None)
    # A failed attempt was sent, so it still counts against the window.
    assert_held(pacer, clock, request(size=60))


def _a_throttle_hint_holds_every_caller_of_the_pacer() -> None:
    clock = FakeClock()
    pacer = IntervalPacer(
        interval_seconds=1.0, requests_per_interval=100, clock=clock, poll_seconds=0.001
    )
    pacer.settle(
        pacer.acquire(request(), deadline=None, cancelled=None), "throttled", retry_after=5.0
    )
    assert_held(pacer, clock, request("query"))
    clock.advance(4.5)
    reservation = pacer.acquire(request("query"), deadline=None, cancelled=None)
    pacer.settle(reservation, "ok", retry_after=None)


def _interval_pacer_validation() -> None:
    cases: list[dict[str, Any]] = [
        {"interval_seconds": 0.0, "requests_per_interval": 1},
        {"interval_seconds": 1.0},
        {"interval_seconds": 1.0, "requests_per_interval": 0},
        {"interval_seconds": 1.0, "bytes_per_interval": 0},
        {"interval_seconds": 1.0, "requests_per_interval": 1, "query_burst": 0},
    ]
    for bad in cases:
        with pytest.raises(IndexError):
            IntervalPacer(**bad)


def _four_threads_share_one_pacer_through_perform_without_lost_settlements() -> None:
    pacer = IntervalPacer(interval_seconds=0.05, requests_per_interval=8, poll_seconds=0.001)
    settled: list[SettleOutcome] = []
    lock = threading.Lock()
    original = pacer.settle

    def counting(reservation: object, outcome: SettleOutcome, *, retry_after: float | None) -> None:
        with lock:
            settled.append(outcome)
        original(reservation, outcome, retry_after=retry_after)

    pacer.settle = counting  # type: ignore[method-assign]
    results: list[str] = []
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        clock = FakeClock()
        send, _ = scripted(throttle(), "ok", throttle(), "ok", "ok")
        try:
            for _ in range(3):
                results.append(
                    perform(
                        send,
                        request=REQUEST,
                        policy=RetryPolicy(),
                        pacer=pacer,
                        rng=random.Random(seed).random,
                        sleep=clock.sleep,
                    )
                )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    workers = [threading.Thread(target=worker, args=(seed,)) for seed in range(4)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == [] and results == ["ok"] * 12
    assert sorted(settled) == sorted(["throttled"] * 8 + ["ok"] * 12)


# --------------------------------------------------------------------------- proof leaves


def test_provider_failures_are_typed_and_keep_accumulated_uncertainty() -> None:
    _typed_errors_are_index_errors_exported_and_merge_toward_evidence_of_work()
    _four_throttles_raise_a_typed_rejected_throttle_with_the_adapter_message()
    _a_timeout_before_a_throttle_keeps_the_request_uncertain()
    _a_timeout_then_a_pacer_refusal_keeps_the_uncertainty_and_sends_nothing_more()
    _a_non_retryable_refusal_stops_at_one_attempt_and_unexpected_errors_pass_through()
    _attempt_failure_refuses_an_unusable_hint()


def test_retry_waits_are_jittered_and_never_retry_before_a_provider_hint() -> None:
    _jittered_delays_stay_inside_their_bounds_and_differ_between_seeds()
    _a_provider_hint_is_a_floor_never_lowered_and_a_long_hint_stops_retrying()
    _a_hint_beyond_the_cap_ends_the_request_without_an_early_retry()
    _a_wait_that_would_cross_the_deadline_stops_now_with_the_provider_kind()
    _the_retry_budget_bounds_the_whole_request()
    _retry_policy_validation()
    _an_overslept_backoff_or_late_grant_never_starts_an_attempt_past_the_budget()
    _no_pacer_no_scope_throttle_then_success_is_two_sends_and_one_jittered_sleep()
    _parse_retry_after_reads_seconds_and_dates_never_dropping_a_future_date()


def test_perform_paces_and_settles_every_physical_attempt_exactly_once() -> None:
    _the_pacer_sees_one_acquire_and_one_settle_per_physical_attempt()
    _a_refusing_pacer_makes_no_request_and_reports_not_sent()
    _a_settle_failure_never_discards_a_paid_answer_or_replaces_a_failure()
    _send_receives_remaining_time_pacer_wait_and_previous_backoff()
    _four_threads_share_one_pacer_through_perform_without_lost_settlements()


def test_call_scopes_bound_every_wait_and_attempt_start() -> None:
    _nested_scopes_take_the_earliest_deadline_and_keep_every_predicate()
    _a_raising_cancellation_predicate_is_a_distinct_interruption()
    _cancellation_during_a_backoff_stops_without_another_attempt()
    _an_expired_scope_starts_no_attempt()
    _an_overslept_backoff_past_the_deadline_starts_nothing_and_keeps_uncertainty()
    _scoped_wait_refuses_a_wait_it_cannot_finish()
    _scoped_lock_waits_no_longer_than_the_scope()
    _a_copied_context_carries_the_scope_into_a_worker_thread()
    _a_pacer_wait_stops_at_the_deadline_inside_perform()


def test_interval_pacer_bounds_rate_reservations_and_capacity() -> None:
    threads: list[threading.Thread] = []
    try:
        _the_reference_pacer_never_exceeds_its_rate_across_many_threads(threads)
        _identical_requests_get_distinct_reservations_settled_exactly_once()
        _oversized_work_is_refused_and_not_sent_capacity_is_returned()
        _a_throttle_hint_holds_every_caller_of_the_pacer()
        _interval_pacer_validation()
    finally:
        for thread in threads:
            thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)


def test_interval_pacer_puts_queries_first_without_starving_documents() -> None:
    threads: list[threading.Thread] = []
    try:
        _a_queued_query_overtakes_documents_and_documents_keep_their_order(threads)
        _documents_progress_under_a_continuous_stream_of_queries(threads)
        _a_cancelled_queue_head_leaves_the_queue_and_the_next_waiter_proceeds(threads)
    finally:
        for thread in threads:
            thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
