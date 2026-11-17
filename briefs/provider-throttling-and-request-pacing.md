---
title: "Provider throttling, retry and request pacing"
date: 2026-11-17
status: draft
scope: "Public technical design and limitations for remote embedding requests; no private corpus, account, quota or deployment evidence is included."
---

# Provider throttling, retry and request pacing

## Problem

A remote embedding provider sheds load by throttling. A consumer that cannot tell a throttle from a genuine failure either spends its own failure retries on throttles or wraps the library in guesses about error text, and several layers each retrying or slicing the same work deepen the throttle. A consumer that shares one provider budget between search and indexing also needs one place where every physical request can be paced.

## Ownership

- The library that makes a physical request owns that request's retry, its jitter, its pacing hook and its typed outcome. Exactly one layer retries.
- A higher library that composes requests into larger operations owns classifying the operation's outcome, stopping new work once the provider is throttling, and truthful accounting. It does not retry physical requests itself.
- The service owns the shared budget, its priorities between search and indexing, and requeueing its own work after a throttle.

## Typed outcome

A failed request reports the latest failure's kind and a delivery state merged over every attempt: not sent, rejected (the service answered with a refusal), unknown (a request may have been processed), or processed (the service did the work but the answer was unusable). Merging keeps the strongest evidence that work was performed, so a later throttle never erases an earlier indeterminate attempt. A consumer decides whether anything may have happened from the delivery state, never from the error's class. The error also carries the attempt count, why retrying stopped, a per-attempt history without payload text, the provider's own retry hint uncapped, and how many leading inputs of the same call were already answered.

## Retry

Waits are a capped exponential jittered downward. A provider's hint is a floor that jitter never lowers. When the hint exceeds the longest permitted wait, or a wait would cross the caller's deadline or the request's budget, the request ends at once with the hint instead of retrying early. Validation failures and other definite refusals are not retried.

## Pacing

A pacer is consulted before every physical attempt, retries included, with the purpose (document or query), the input count and the request's conservative size in UTF-8 bytes, and is told each attempt's outcome exactly once. The byte size is not a token count and not a rate; enforcing a token quota needs model-specific token counting or a justified conservative bound, which post-response usage cannot provide. One pacer object stands for one quota domain and coordinates only the threads of one process that share it; it coordinates nothing across processes or hosts. A small reference pacer limits requests and bytes per sliding interval, puts queries first while guaranteeing bounded progress for documents, and carries no default numbers: limits come from the operator's own measurement for a named model, Region, endpoint and account.

## Deadlines and cancellation

A caller scope carries an absolute monotonic deadline and cancellation predicates; nested scopes keep the earliest deadline and every predicate. The scope bounds the pacer wait, backoff, the start of further attempts, waits on shared initialization and each socket read. It does not bound the whole duration of an attempt already in flight, credential resolution in a provider SDK, a custom pacer that ignores its deadline, or an embedder that bypasses the shared retry loop. Timeouts are applied per request; a client shared by concurrent calls is never reconfigured.

## Accounting

Every physical attempt is counted where it is made, including throttled and indeterminate attempts, in a collector that a caller scopes to its own operation. Work handed to worker threads carries the caller's context through a fresh copy per task; only the thread-safe collector object is shared, so enclosing totals include nested ones and must be read one level at a time.

## Deferred

Keeping already-answered slices across calls; aligning a composing library's slices with the provider's request shape; other retry loops in the library that do not serve embedding quota; renaming the older byte budget constant whose name says tokens; coordination across processes or hosts; live throttle qualification against a real provider.
