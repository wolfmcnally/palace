"""Exception type for the index surface.

Lifted into its own module so :mod:`palace.index.cli`,
:mod:`palace.index.schema`, :mod:`palace.index.embedder`, and
:mod:`palace.index.server` all import the same base without forming an
import cycle. The CLI shim translates :class:`IndexError` into a single
``error: <message>`` line on stderr with exit 1, matching the
``error:`` convention Phase 1.4 / 1.5 settled on for
:class:`palace.hooks._common.HooksError`, Phase 2.1 for
:class:`palace.watch._errors.WatchError`, and Phase 2.2 for
:class:`palace.reindex._errors.ReindexError`.

This name intentionally shadows Python's builtin ``IndexError`` *only*
inside :mod:`palace.index`. Code in this subpackage never indexes into
out-of-bounds sequences in a way that would care; the shadow is the
explicit phase choice (see Phase 2.3 Open Questions / Minor Corrections).
"""

from __future__ import annotations

import dataclasses
from typing import Literal, Self

__all__ = [
    "DELIVERY_ORDER",
    "AttemptRecord",
    "Delivery",
    "FailureKind",
    "IdentityMismatchError",
    "IndexError",
    "ProviderCallError",
    "ProviderThrottledError",
    "StalePlanError",
    "StopReason",
    "merge_delivery",
]

#: What is known about whether a provider received and acted on a request.
#:
#: - ``not-sent``: no request left the process (never connected, or stopped before sending).
#: - ``rejected``: the service answered with a refusal; it did not perform the work.
#: - ``unknown``: a request may have been processed (read timeout, connection closed mid-call).
#: - ``processed``: the service performed the work and answered, but the answer was unusable.
#:
#: Nothing here claims anything about billing.
Delivery = Literal["not-sent", "rejected", "unknown", "processed"]

#: Ascending strength of evidence that the provider performed work. Merging two states keeps
#: the stronger one, so an indeterminate attempt is never erased by a later definite refusal.
DELIVERY_ORDER: tuple[Delivery, ...] = ("not-sent", "rejected", "unknown", "processed")

#: The latest failure's classification. Bounded vocabulary; never payload text.
FailureKind = Literal[
    "throttled",
    "unavailable",
    "timeout",
    "connection",
    "rejected",
    "response-invalid",
    "deadline",
    "cancelled",
    "cancellation-failed",
    "pacer-refused",
]

#: Why no further attempt was made for the failing request.
StopReason = Literal[
    "not-retryable",
    "attempts-exhausted",
    "hint-beyond-cap",
    "beyond-deadline",
    "beyond-budget",
    "interrupted",
    "pacer-refused",
]


def merge_delivery(first: Delivery, second: Delivery) -> Delivery:
    """The stronger of two delivery states under :data:`DELIVERY_ORDER`."""
    return first if DELIVERY_ORDER.index(first) >= DELIVERY_ORDER.index(second) else second


class IndexError(Exception):  # noqa: A001 — intentional shadow per phase
    """Base exception for the index surface.

    Carries a single-line message; the ``palace index serve`` entry point
    translates this into ``error: ...`` on stderr and exits 1 without a
    Python traceback.
    """


class IdentityMismatchError(IndexError):
    """The store certificate differs from the selected embedding identity."""


class StalePlanError(IndexError):
    """A file plan's prior state changed under it before its commit.

    Raised inside the commit transaction after the writer lock is held, so
    the caller re-plans against the store as it is now rather than committing
    rows derived from a prior state another writer has already replaced.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One physical attempt (or one refused or interrupted wait) of a failing request."""

    kind: FailureKind
    delivery: Delivery
    code: str
    retry_after: float | None


class ProviderCallError(IndexError):
    """A remote provider request failed; the message stays the adapter's single line.

    ``kind`` classifies the latest failure. ``delivery`` is merged across every attempt of
    the failing request (:func:`merge_delivery`), so a read timeout followed by a throttle
    reports ``unknown``, not ``rejected``. ``attempts`` counts physical requests made for the
    failing request; ``history`` lists each attempt's classification and, last, any refused
    or interrupted wait. ``retry_after`` is the provider's latest hint in seconds, measured
    from the receipt of the response that carried it and never capped; ``retry_not_before``
    is that instant on this process's monotonic clock. ``completed_inputs`` counts the
    leading inputs of the same ``embed`` call whose requests succeeded before this one; their
    vectors are not returned.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        operation: str,
        kind: FailureKind,
        delivery: Delivery,
        code: str,
        attempts: int,
        stop: StopReason,
        history: tuple[AttemptRecord, ...] = (),
        retry_after: float | None = None,
        retry_not_before: float | None = None,
        completed_inputs: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.operation = operation
        self.kind: FailureKind = kind
        self.delivery: Delivery = delivery
        self.code = code
        self.attempts = attempts
        self.stop: StopReason = stop
        self.history = history
        self.retry_after = retry_after
        self.retry_not_before = retry_not_before
        self.completed_inputs = completed_inputs

    @property
    def throttled(self) -> bool:
        """Whether any attempt of the failing request was throttled."""
        return self.kind == "throttled" or any(item.kind == "throttled" for item in self.history)

    def with_completed_inputs(self, completed_inputs: int) -> Self:
        """A copy of this error with ``completed_inputs`` set; the message is unchanged."""
        return type(self)(
            self.message,
            provider=self.provider,
            operation=self.operation,
            kind=self.kind,
            delivery=self.delivery,
            code=self.code,
            attempts=self.attempts,
            stop=self.stop,
            history=self.history,
            retry_after=self.retry_after,
            retry_not_before=self.retry_not_before,
            completed_inputs=completed_inputs,
        )


class ProviderThrottledError(ProviderCallError):
    """The latest failure of the request was a provider throttle.

    ``delivery`` keeps any earlier uncertainty: a throttle after a read timeout is still
    ``unknown``. A caller deciding whether anything may have been performed reads
    ``delivery``, never the class.
    """
