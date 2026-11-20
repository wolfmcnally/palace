---
id: typed-provider-failures
title: Typed provider failures, one retry loop and request pacing
depends_on:
- unloadable-front-matter
informs:
- release-zero-six
review_lane: full
order: '14'
parent: null
---

# Typed provider failures, one retry loop and request pacing

## Commission

Authorized on 2026-11-17 by the operator, relayed through the authenticated factotum on the coordination topic: comprehensive embedding throttling and concurrency improvements in Palace and every owned dependent that needs them, with tests, and tagged releases so a deployed multi-store consumer that shares one provider budget between search and indexing can integrate without guessing at throttling from error text. The operator stated "release everything you need to touch that we own"; no live provider or cloud call, no secret or security change. The work was planned outside the repository and reviewed by a cross-provider plan reviewer (Astra, medium) before code; its corrections are incorporated below. Implementation is split between two writers with disjoint files under one coordinator: a core lane (`palace/index/_errors.py`, `palace/index/pacing.py`, `palace/index/__init__.py`, `tests/index/test_pacing.py`, phase records and integration) and an adapter lane (`palace/bedrock.py`, `palace/index/embedder.py`, `palace/index/embedder_config.py`, `palace/index/egress.py`, `palace/index/config.py` and their provider tests). Code critique is by the coordinator's cross-provider reviewer.

Found at baseline `b1a3944f2466`: Bedrock and OpenRouter each run their own exponential-backoff loop without jitter; Bedrock ignores `Retry-After` and OpenRouter caps it downward, so a long hint causes an early retry; terminal failures are plain `IndexError` text, so a throttle is indistinguishable from a denied credential; backoff sleeps cannot be bounded or cancelled; a multi-request `embed` that fails late does not say how many inputs were already answered; and nothing paces request starts or shares a budget between embedders. The 120,000 request cap is in conservative UTF-8 bytes, not tokens, and is not a rate.

## Goal

Every remote embedding request ends in a typed, truthful outcome; one retry loop paces, backs off with jitter, honors a provider's hint without ever retrying early, and stops at the caller's deadline or cancellation; a caller-supplied pacer, shared across embedders in one process, sees every physical attempt.

## Deliverables

- `ProviderCallError` and `ProviderThrottledError` (both `IndexError`), carrying the latest failure kind, the delivery state merged across every attempt (an indeterminate attempt is never erased by a later refusal), attempts, stop reason, per-attempt history, the provider's uncapped hint and its monotonic instant, and the inputs already answered in the same call.
- `palace.index.pacing`: `RetryPolicy` (capped, jittered exponential; a hint is a floor and a hint beyond the cap stops at once), `parse_retry_after`, `PacedRequest` and the `RequestPacer` protocol with reservations settled exactly once, `perform` as the single retry loop, `call_scope` with earliest-deadline nesting and interruptible waits and lock acquisition, and a small `IntervalPacer` reference (requests and bytes per sliding interval, queries first with bounded document progress, cancellation removal, oversized refusal, hint hold, no default numbers).
- Both remote embedders use `perform`, accept a pacer and a retry policy, bound transport reads by the remaining scope time without mutating a shared client, and add pacer wait and backoff to their audit records; `resolve_embedder` passes a pacer and a policy through.
- Usage accounting counts throttled and indeterminate attempts.
- Focused proofs for each, admitted to the proof estate.

## Acceptance

- Every existing proof passes; the Bedrock retry assertion becomes jitter-aware and nothing else changes meaning.
- A read timeout followed by throttles reports delivery `unknown`; a 120-second hint with a 30-second cap ends after one attempt with the hint and no sleep; a wait that would cross the deadline stops without sleeping; a cancelled backoff starts no further attempt.
- A pacer sees one acquire and one settle per physical attempt with purpose, input count and byte size; a refusing pacer sends nothing and reports `not-sent`; a raising settle never discards a paid answer.
- The reference pacer never exceeds its window across threads, lets a waiting query overtake documents, still admits a waiting document after the configured burst of queries, removes a cancelled waiter and refuses oversized work.
- Mutating delivery merging, the hint cap, query priority, the document guard, capacity refund, the throttle hold, scope nesting or the deadline stop fails a proof.
- Focused tests and both full gates pass against their fixed candidates, with the code critique's dispositions retained.
- Manual: whether the contract meets the deployed consumer's need belongs to the operator and that consumer.

User Demo: N/A — library behavior proved by deterministic synthetic tests; no live provider call.

## Exclusions

- A version change, changelog release section, release documentation or tag (Phase 34).
- The private-inference adapter's own retry loop and the S3 Vectors backoff loop.
- Coordination across processes or hosts; any default rate; token-quota enforcement.
- Renaming `REMOTE_EMBED_MAX_TOKEN_BUDGET`; its documentation states its byte unit.
- Live throttle qualification.

## Brief refs

- [bedrock embedding provider](../../briefs/bedrock-embedding-provider.md)
- [remote embedding throughput](../../briefs/remote-embedding-throughput.md)
- [deployed consumer readiness](../../briefs/deployed-consumer-readiness.md)
