"""Retry, pacing, deadline and cancellation for remote provider requests.

Palace owns every physical request a remote embedder makes, so it owns the single retry
loop (:func:`perform`), the point where each attempt is paced (:class:`RequestPacer`), and
the typed failure each request ends in (:class:`palace.index.ProviderCallError`).

Units. :attr:`PacedRequest.request_bytes` is Palace's conservative request size: for each
input the larger of its raw and NFC-normalized UTF-8 byte lengths, plus eight bytes of
framing allowance. It is not a token count, not the exact wire size, and not a rate. A
pacer that enforces a byte or request budget enforces exactly that; turning it into a token
quota needs model-specific token counting or a justified conservative bound, which this
module does not provide.

Scope. A pacer coordinates the threads of one process that share the same pacer object. It
coordinates nothing across processes or hosts. One pacer stands for one quota domain
(provider, model, Region, account); a caller builds one per domain and hands the same
object to every embedder that draws on it.

Deadlines and cancellation (:func:`call_scope`) bound: the wait before an attempt (scope
check, pacer wait, the reference pacer's queue), every backoff sleep, and whether another
attempt starts. They do not interrupt an attempt already in flight; that is bounded only
by the adapter's own transport timeouts, which an adapter may shorten to the remaining time
but must not change on a client shared by concurrent calls. A custom pacer receives the
deadline and cancellation predicate and is responsible for honoring them; a custom
embedder that does not call :func:`perform` is bounded by nothing here.
"""

from __future__ import annotations

import dataclasses
import math
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol

from palace.index._errors import (
    AttemptRecord,
    Delivery,
    FailureKind,
    IndexError,
    ProviderCallError,
    ProviderThrottledError,
    StopReason,
    merge_delivery,
)
from palace.index.config import (
    REMOTE_EMBED_BACKOFF_BASE_SECONDS,
    REMOTE_EMBED_BACKOFF_MAX_SECONDS,
    REMOTE_EMBED_MAX_ATTEMPTS,
)

__all__ = [
    "CANCEL_POLL_SECONDS",
    "AttemptFailure",
    "AttemptInfo",
    "CallScope",
    "IntervalPacer",
    "PacedRequest",
    "PacerRefused",
    "PacerReservation",
    "Purpose",
    "RequestPacer",
    "RetryPolicy",
    "SettleOutcome",
    "WaitInterrupted",
    "call_scope",
    "check_scope",
    "current_scope",
    "parse_retry_after",
    "perform",
    "remaining_seconds",
    "scoped_lock",
    "scoped_wait",
]

Purpose = Literal["document", "query"]
SettleOutcome = Literal["ok", "throttled", "failed", "not-sent"]
Interruption = Literal["deadline", "cancelled", "cancellation-failed"]

#: How often an interruptible wait re-reads a cancellation predicate.
CANCEL_POLL_SECONDS: float = 0.05

_RNG = random.Random()


# --------------------------------------------------------------------------- scope


class WaitInterrupted(Exception):  # noqa: N818 — a signal, not a failure of this module
    """A wait stopped because the deadline passed or the caller cancelled.

    ``reason`` is ``deadline``, ``cancelled`` or ``cancellation-failed`` (the caller's
    predicate raised; that exception is the ``__cause__``).
    """

    def __init__(self, reason: Interruption) -> None:
        super().__init__(reason)
        self.reason: Interruption = reason


@dataclasses.dataclass(frozen=True, slots=True)
class CallScope:
    """An absolute monotonic deadline and the caller's cancellation predicates."""

    deadline: float | None = None
    cancelled: tuple[Callable[[], bool], ...] = ()

    def remaining(self, now: float) -> float | None:
        """Seconds until the deadline (never negative), or ``None`` when unbounded."""
        return None if self.deadline is None else max(0.0, self.deadline - now)

    def check(self, now: float) -> None:
        """Raise :class:`WaitInterrupted` if the deadline has passed or any predicate is true."""
        if self.deadline is not None and now >= self.deadline:
            raise WaitInterrupted("deadline")
        for predicate in self.cancelled:
            try:
                stop = predicate()
            except Exception as error:  # noqa: BLE001 — a failing predicate stops the wait
                raise WaitInterrupted("cancellation-failed") from error
            if stop:
                raise WaitInterrupted("cancelled")

    def predicate(self) -> Callable[[], bool] | None:
        """One predicate combining every cancellation predicate, or ``None``."""
        if not self.cancelled:
            return None
        predicates = self.cancelled
        return lambda: any(predicate() for predicate in predicates)


_UNBOUNDED = CallScope()
_SCOPE: ContextVar[CallScope | None] = ContextVar("palace_index_call_scope", default=None)


def _active() -> CallScope:
    return _SCOPE.get() or _UNBOUNDED


@contextmanager
def call_scope(
    *, deadline: float | None = None, cancelled: Callable[[], bool] | None = None
) -> Iterator[CallScope]:
    """Bound remote requests made in this context by ``deadline`` and ``cancelled``.

    ``deadline`` is an absolute :func:`time.monotonic` instant. Nested scopes combine: the
    earlier deadline wins and every enclosing predicate still applies, so an inner scope can
    only tighten an outer one. Like every context variable, the scope reaches a worker
    thread only when the work runs in a copy of the submitting context; copy it once per
    submitted task, never enter one copied context from two threads.
    """
    outer = _active()
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("call_scope deadline must be finite")
    combined = CallScope(
        deadline=(
            deadline
            if outer.deadline is None
            else outer.deadline
            if deadline is None
            else min(outer.deadline, deadline)
        ),
        cancelled=outer.cancelled + ((cancelled,) if cancelled is not None else ()),
    )
    token = _SCOPE.set(combined)
    try:
        yield combined
    finally:
        _SCOPE.reset(token)


def current_scope() -> CallScope:
    """The scope active in this context; unbounded when none was opened."""
    return _active()


def check_scope(*, clock: Callable[[], float] = time.monotonic) -> None:
    """Raise :class:`WaitInterrupted` if the active scope's deadline passed or it cancelled."""
    _active().check(clock())


def remaining_seconds(*, clock: Callable[[], float] = time.monotonic) -> float | None:
    """Seconds left in the active scope, or ``None`` when it has no deadline."""
    return _active().remaining(clock())


def scoped_wait(
    seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait ``seconds`` unless the active scope ends first.

    Raises :class:`WaitInterrupted` at once, without sleeping, when the wait cannot finish
    before the deadline. With no cancellation predicate the wait is one ``sleep`` call;
    otherwise the predicates are re-read every :data:`CANCEL_POLL_SECONDS`.
    """
    scope = _active()
    now = clock()
    scope.check(now)
    remaining = scope.remaining(now)
    if remaining is not None and seconds >= remaining:
        raise WaitInterrupted("deadline")
    if not scope.cancelled:
        if seconds > 0:
            sleep(seconds)
        return
    end = now + seconds
    while True:
        left = end - clock()
        if left <= 0:
            return
        sleep(min(left, CANCEL_POLL_SECONDS))
        scope.check(clock())


@contextmanager
def scoped_lock(
    lock: threading.Lock | threading.RLock,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Hold ``lock``, waiting no longer than the active scope allows.

    For contention on shared initialization: a caller queued behind a slow first request
    stops at its own deadline or cancellation instead of waiting for the holder.
    """
    scope = _active()
    while True:
        now = clock()
        scope.check(now)
        remaining = scope.remaining(now)
        timeout = CANCEL_POLL_SECONDS if scope.cancelled else remaining
        if remaining is not None and timeout is not None:
            timeout = min(timeout, remaining)
        if lock.acquire(timeout=-1 if timeout is None else max(timeout, 0.0)):
            break
    try:
        yield
    finally:
        lock.release()


# --------------------------------------------------------------------------- retry policy


@dataclasses.dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How one request is retried.

    ``max_wait_seconds`` caps the exponential component of any single wait. A provider's
    ``Retry-After`` hint is a floor that jitter never lowers; a hint longer than
    ``max_wait_seconds`` ends the request at once with that hint, rather than retrying
    early. ``jitter`` is the fraction of the exponential delay drawn away at random
    (``0`` disables it). ``budget_seconds`` optionally bounds the whole request, from its
    first wait to its last attempt's start.
    """

    max_attempts: int = REMOTE_EMBED_MAX_ATTEMPTS
    base_seconds: float = REMOTE_EMBED_BACKOFF_BASE_SECONDS
    max_wait_seconds: float = REMOTE_EMBED_BACKOFF_MAX_SECONDS
    jitter: float = 0.5
    budget_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise IndexError(f"max_attempts must be at least 1; got {self.max_attempts}")
        for name in ("base_seconds", "max_wait_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise IndexError(f"{name} must be finite and non-negative; got {value}")
        if not 0.0 <= self.jitter <= 1.0:
            raise IndexError(f"jitter must be between 0 and 1; got {self.jitter}")
        if self.budget_seconds is not None and (
            not math.isfinite(self.budget_seconds) or self.budget_seconds <= 0
        ):
            raise IndexError(
                f"budget_seconds must be finite and positive; got {self.budget_seconds}"
            )

    def delay(
        self, attempt: int, retry_after: float | None, *, rng: Callable[[], float]
    ) -> float | None:
        """The wait after failed ``attempt`` (1-based), or ``None`` to stop now.

        The exponential component is capped, then jittered downward; the provider hint is
        applied last as a floor, spread upward by the same jitter fraction but never beyond
        the cap. ``None`` means the hint exceeds the cap.
        """
        exponential = min(self.max_wait_seconds, self.base_seconds * 2.0 ** (attempt - 1))
        jittered = exponential * (1.0 - self.jitter * rng())
        if retry_after is None:
            return jittered
        if retry_after > self.max_wait_seconds:
            return None
        floor = min(self.max_wait_seconds, retry_after * (1.0 + self.jitter * rng()))
        return max(jittered, floor)


def parse_retry_after(
    value: str | None,
    *,
    date: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> float | None:
    """A ``Retry-After`` header value as finite, non-negative seconds, or ``None``.

    Delta-seconds are read directly. An HTTP-date is measured against the same response's
    ``Date`` header; when that is missing or unreadable it is measured against ``now()``
    (default: this host's UTC clock), so a future hint is never dropped. A date without a
    zone is read as UTC. A date already past gives ``0``. Anything unreadable, negative or
    non-finite gives ``None``.
    """
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        target = _http_date(text)
        if target is None:
            return None
        sent = _http_date(date.strip()) if date is not None else None
        if sent is None:
            sent = (now if now is not None else _utc_now)()
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
        try:
            seconds = max(0.0, (target - sent).total_seconds())
        except (TypeError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _http_date(text: str) -> datetime | None:
    """An RFC 9110 HTTP-date as an aware datetime, or ``None`` when unreadable."""
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- pacer protocol


@dataclasses.dataclass(frozen=True, slots=True)
class PacedRequest:
    """One physical request about to be made, as a pacer sees it. Never carries text."""

    provider: str
    model: str
    operation: str
    purpose: Purpose
    inputs: int
    #: Palace's conservative size in UTF-8 bytes (larger of raw/NFC per input, plus eight
    #: bytes framing allowance per input). Not tokens; not a rate.
    request_bytes: int
    attempt: int = 1


class PacerRefused(Exception):  # noqa: N818 — the pacer's own refusal signal
    """A pacer will never admit this request (for example, it exceeds the whole budget)."""


class RequestPacer(Protocol):
    """Paces physical requests across every caller sharing this object in one process.

    ``acquire`` blocks until the request may start and returns a reservation that is passed
    to ``settle`` exactly once. It must be thread-safe, blocking and event-loop free, and
    must honor ``deadline`` (absolute monotonic) and ``cancelled`` by raising
    :class:`WaitInterrupted`; any other exception refuses the request. ``settle`` reports
    the attempt: ``ok``, ``throttled``, ``failed`` (sent, then failed) or ``not-sent``
    (admitted but never sent, so its capacity may be returned). ``retry_after`` is the
    provider's uncapped hint. ``settle`` should not raise; if it does, the attempt's own
    outcome stands.
    """

    def acquire(
        self,
        request: PacedRequest,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> object: ...

    def settle(
        self, reservation: object, outcome: SettleOutcome, *, retry_after: float | None
    ) -> None: ...


# --------------------------------------------------------------------------- the retry loop


class AttemptFailure(Exception):  # noqa: N818 — an adapter's classification of one attempt
    """Raised by an adapter's ``send`` to classify one failed physical attempt.

    ``message`` is the single line the request's terminal error carries. ``retry_after``
    must be finite and non-negative seconds from receipt of the response, or ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind,
        delivery: Delivery,
        code: str,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError(f"retry_after must be finite and non-negative; got {retry_after}")
        super().__init__(message)
        self.message = message
        self.kind: FailureKind = kind
        self.delivery: Delivery = delivery
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclasses.dataclass(frozen=True, slots=True)
class AttemptInfo:
    """What ``send`` knows about the attempt it is about to make, for its audit record.

    ``pacer_wait_seconds`` is measured; ``backoff_seconds`` is the backoff requested before
    this attempt (a sleep may run longer, but never past the retry budget or deadline).
    """

    number: int
    remaining_seconds: float | None
    pacer_wait_seconds: float
    backoff_seconds: float


_SETTLE_FOR: dict[FailureKind, SettleOutcome] = {"throttled": "throttled"}


def perform[T](
    send: Callable[[AttemptInfo], T],
    *,
    request: PacedRequest,
    policy: RetryPolicy,
    pacer: RequestPacer | None = None,
    completed_inputs: int = 0,
    rng: Callable[[], float] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Make one logical request: pace, send, classify, back off and retry, or raise typed.

    ``send`` makes exactly one physical attempt. It returns the answer, or raises
    :class:`AttemptFailure`; any other exception propagates unchanged after the pacer is
    settled ``failed``. The terminal :class:`ProviderCallError` carries the latest kind,
    the delivery merged over every attempt, and the provider's latest uncapped hint.
    """
    draw = rng if rng is not None else _RNG.random
    history: list[AttemptRecord] = []
    state: dict[str, object] = {}
    delivery: Delivery = "not-sent"
    attempts = 0
    retry_after: float | None = None
    retry_not_before: float | None = None
    budget_end = None if policy.budget_seconds is None else clock() + policy.budget_seconds
    backoff = 0.0
    last: AttemptFailure | None = None

    def fail(
        message: str,
        kind: FailureKind,
        code: str,
        stop: StopReason,
    ) -> ProviderCallError:
        error_type = ProviderThrottledError if kind == "throttled" else ProviderCallError
        error = error_type(
            message,
            provider=request.provider,
            operation=request.operation,
            kind=kind,
            delivery=delivery,
            code=code,
            attempts=attempts,
            stop=stop,
            history=tuple(history),
            retry_after=retry_after,
            retry_not_before=retry_not_before,
            completed_inputs=completed_inputs,
        )
        note = state.get("settle_failed")
        if isinstance(note, str):
            error.add_note(note)
        return error

    def interrupted(signal: WaitInterrupted, where: str) -> ProviderCallError:
        history.append(AttemptRecord(signal.reason, "not-sent", signal.reason, None))
        return fail(
            f"{request.provider} {request.operation} stopped by {signal.reason} {where} "
            f"after {attempts} attempt(s)",
            signal.reason,
            signal.reason,
            "interrupted",
        )

    def budget_spent(where: str) -> ProviderCallError:
        # A sleep or pacer grant can overrun its request; no attempt starts past the budget.
        if last is not None:
            return fail(last.message, last.kind, last.code, "beyond-budget")
        history.append(AttemptRecord("deadline", "not-sent", "budget", None))
        return fail(
            f"{request.provider} {request.operation} retry budget ended {where} "
            f"after {attempts} attempt(s)",
            "deadline",
            "budget",
            "beyond-budget",
        )

    def settle(reservation: object, outcome: SettleOutcome, hint: float | None) -> None:
        if pacer is None:
            return
        try:
            pacer.settle(reservation, outcome, retry_after=hint)
        except Exception as error:  # noqa: BLE001 — never replaces the attempt's outcome
            state["settle_failed"] = f"pacer settle failed: {type(error).__name__}"

    for number in range(1, policy.max_attempts + 1):
        paced = dataclasses.replace(request, attempt=number)
        scope = _active()
        try:
            scope.check(clock())
        except WaitInterrupted as signal:
            raise interrupted(signal, "before the attempt") from signal.__cause__
        if budget_end is not None and clock() >= budget_end:
            raise budget_spent("before the attempt") from None
        waited_from = clock()
        reservation: object = None
        if pacer is not None:
            deadline = scope.deadline
            if budget_end is not None:
                deadline = budget_end if deadline is None else min(deadline, budget_end)
            try:
                reservation = pacer.acquire(paced, deadline=deadline, cancelled=scope.predicate())
            except WaitInterrupted as signal:
                if signal.reason == "deadline" and (
                    scope.deadline is None or clock() < scope.deadline
                ):
                    # Only the request's own budget ran out; the caller's deadline stands.
                    raise budget_spent("in the pacer wait") from None
                raise interrupted(signal, "in the pacer wait") from signal.__cause__
            except Exception as error:
                history.append(
                    AttemptRecord("pacer-refused", "not-sent", type(error).__name__, None)
                )
                raise fail(
                    f"{request.provider} {request.operation} refused by the request pacer "
                    f"after {attempts} attempt(s): {type(error).__name__}",
                    "pacer-refused",
                    type(error).__name__,
                    "pacer-refused",
                ) from error
        pacer_wait = clock() - waited_from
        try:
            scope.check(clock())
        except WaitInterrupted as signal:
            settle(reservation, "not-sent", None)
            raise interrupted(signal, "before the attempt") from signal.__cause__
        if budget_end is not None and clock() >= budget_end:
            settle(reservation, "not-sent", None)
            raise budget_spent("before the attempt") from None
        attempts += 1
        info = AttemptInfo(number, scope.remaining(clock()), pacer_wait, backoff)
        try:
            answer = send(info)
        except AttemptFailure as failure:
            received = clock()
            last = failure
            delivery = merge_delivery(delivery, failure.delivery)
            history.append(
                AttemptRecord(failure.kind, failure.delivery, failure.code, failure.retry_after)
            )
            if failure.retry_after is not None:
                retry_after = failure.retry_after
                retry_not_before = received + failure.retry_after
            outcome = _SETTLE_FOR.get(
                failure.kind, "not-sent" if failure.delivery == "not-sent" else "failed"
            )
            settle(reservation, outcome, failure.retry_after)
            if not failure.retryable:
                raise fail(failure.message, failure.kind, failure.code, "not-retryable") from None
            if number == policy.max_attempts:
                raise fail(
                    failure.message, failure.kind, failure.code, "attempts-exhausted"
                ) from None
            wait = policy.delay(number, failure.retry_after, rng=draw)
            if wait is None:
                raise fail(failure.message, failure.kind, failure.code, "hint-beyond-cap") from None
            now = clock()
            remaining = scope.remaining(now)
            if remaining is not None and wait >= remaining:
                raise fail(failure.message, failure.kind, failure.code, "beyond-deadline") from None
            if budget_end is not None and now + wait >= budget_end:
                raise fail(failure.message, failure.kind, failure.code, "beyond-budget") from None
            try:
                scoped_wait(wait, clock=clock, sleep=sleep)
            except WaitInterrupted as signal:
                raise interrupted(signal, "in the backoff") from signal.__cause__
            backoff = wait
            continue
        except BaseException:
            settle(reservation, "failed", None)
            raise
        settle(reservation, "ok", None)
        return answer
    raise AssertionError("unreachable: the final attempt returns or raises")


# --------------------------------------------------------------------------- reference pacer


@dataclasses.dataclass(eq=False, slots=True)
class PacerReservation:
    """One admission by an :class:`IntervalPacer`; settled exactly once."""

    request: PacedRequest
    granted_at: float
    owner: IntervalPacer
    settled: bool = False


class IntervalPacer:
    """A small generic reference pacer: requests and bytes per sliding interval.

    No default numbers: the caller supplies limits from its own measurement. Within any
    window of ``interval_seconds``, admitted requests never exceed ``requests_per_interval``
    and their ``request_bytes`` never exceed ``bytes_per_interval``. Every admitted request
    counts against the window whether it succeeds or fails; one settled ``not-sent`` returns
    its capacity. A ``throttled`` settlement with a hint holds every caller of this pacer
    until the hint expires. A request larger than ``bytes_per_interval`` is refused at once.

    Two classes queue first-in-first-out. A waiting query goes before waiting documents,
    but after ``query_burst`` consecutive query admissions while a document waits, the
    oldest document goes next, so documents keep progressing under sustained queries.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        requests_per_interval: int | None = None,
        bytes_per_interval: int | None = None,
        query_burst: int = 4,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = CANCEL_POLL_SECONDS,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise IndexError(f"interval_seconds must be positive; got {interval_seconds}")
        if requests_per_interval is None and bytes_per_interval is None:
            raise IndexError("an IntervalPacer needs requests_per_interval or bytes_per_interval")
        for name, value in (
            ("requests_per_interval", requests_per_interval),
            ("bytes_per_interval", bytes_per_interval),
        ):
            if value is not None and value < 1:
                raise IndexError(f"{name} must be at least 1; got {value}")
        if query_burst < 1:
            raise IndexError(f"query_burst must be at least 1; got {query_burst}")
        if not poll_seconds > 0:
            raise IndexError(f"poll_seconds must be positive; got {poll_seconds}")
        self._interval = interval_seconds
        self._max_requests = requests_per_interval
        self._max_bytes = bytes_per_interval
        self._query_burst = query_burst
        self._clock = clock
        self._poll = poll_seconds
        self._condition = threading.Condition()
        self._window: deque[PacerReservation] = deque()
        self._window_bytes = 0
        self._queues: dict[Purpose, deque[object]] = {"query": deque(), "document": deque()}
        self._streak = 0
        self._hold_until: float | None = None

    def acquire(
        self,
        request: PacedRequest,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> PacerReservation:
        if self._max_bytes is not None and request.request_bytes > self._max_bytes:
            raise PacerRefused(
                f"request of {request.request_bytes} bytes exceeds bytes_per_interval "
                f"{self._max_bytes}"
            )
        ticket = object()
        queue = self._queues[request.purpose]
        with self._condition:
            queue.append(ticket)
            try:
                while True:
                    now = self._clock()
                    if deadline is not None and now >= deadline:
                        raise WaitInterrupted("deadline")
                    if cancelled is not None:
                        try:
                            stop = cancelled()
                        except Exception as error:  # noqa: BLE001
                            raise WaitInterrupted("cancellation-failed") from error
                        if stop:
                            raise WaitInterrupted("cancelled")
                    self._expire(now)
                    if self._next() is ticket and self._fits(request, now):
                        queue.popleft()
                        return self._grant(request, now)
                    wait = self._poll
                    if deadline is not None:
                        wait = min(wait, max(deadline - now, 0.0))
                    self._condition.wait(max(wait, 1e-4))
            except BaseException:
                if ticket in queue:
                    queue.remove(ticket)
                self._condition.notify_all()
                raise

    def settle(
        self, reservation: object, outcome: SettleOutcome, *, retry_after: float | None
    ) -> None:
        if not isinstance(reservation, PacerReservation) or reservation.owner is not self:
            raise ValueError("reservation was not issued by this pacer")
        with self._condition:
            if reservation.settled:
                raise ValueError("reservation already settled")
            reservation.settled = True
            if outcome == "not-sent":
                for index, held in enumerate(self._window):
                    if held is reservation:
                        del self._window[index]
                        self._window_bytes -= reservation.request.request_bytes
                        break
            elif outcome == "throttled" and retry_after is not None and retry_after > 0:
                until = self._clock() + retry_after
                if self._hold_until is None or until > self._hold_until:
                    self._hold_until = until
            self._condition.notify_all()

    # internals, called with the condition held

    def _expire(self, now: float) -> None:
        while self._window and self._window[0].granted_at <= now - self._interval:
            held = self._window.popleft()
            self._window_bytes -= held.request.request_bytes
        if self._hold_until is not None and now >= self._hold_until:
            self._hold_until = None

    def _next(self) -> object | None:
        queries, documents = self._queues["query"], self._queues["document"]
        if queries and (not documents or self._streak < self._query_burst):
            return queries[0]
        if documents:
            return documents[0]
        return None

    def _fits(self, request: PacedRequest, now: float) -> bool:
        if self._hold_until is not None and now < self._hold_until:
            return False
        if self._max_requests is not None and len(self._window) + 1 > self._max_requests:
            return False
        return self._max_bytes is None or self._window_bytes + request.request_bytes <= (
            self._max_bytes
        )

    def _grant(self, request: PacedRequest, now: float) -> PacerReservation:
        if request.purpose == "query":
            self._streak = self._streak + 1 if self._queues["document"] else 0
        else:
            self._streak = 0
        reservation = PacerReservation(request=request, granted_at=now, owner=self)
        self._window.append(reservation)
        self._window_bytes += request.request_bytes
        self._condition.notify_all()
        return reservation
