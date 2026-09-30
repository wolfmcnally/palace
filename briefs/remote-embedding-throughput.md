---
title: "Bounded remote embedding throughput"
date: 2026-09-30
status: draft
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Bounded remote embedding throughput

## Design

Overlap remote waits with bounded request concurrency and bounded queues. Plan inputs, construct embeddings and commit index changes through explicit seams. Do not add local embedder concurrency or cross-file batching without measured justification.

## Measurement procedure

Use disposable, redistributable fixtures. Measure producer time `p`, writer time `w`, network time `n`, actual request count, retries, status classes and cost. For a calling-thread producer/writer pipeline, the ceiling is `(p + w + n) / max(p + w, n / m)` at measured upstream multiple `m`. A topology with off-thread production has a different bound; label it separately.

Measure multiple repetitions and report intervals, workload size, cache state and whether storage costs scale at production size. Precommit a branch threshold before observing results; escalate an interval spanning that threshold instead of choosing by rounding. No private endpoint, corpus, paid-run result or production qualification is claimed here.

## Boundaries

Authorization, embedding identity, audit integrity, bounded retries and caller-visible failures remain mandatory. A throughput improvement cannot spend those invariants. Treat a requested speedup not reached as an unmet criterion.
