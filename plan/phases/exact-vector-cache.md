---
id: exact-vector-cache
title: Opt-in exact in-memory vector cache
depends_on:
- release-zero-six
informs: []
review_lane: full
order: '16'
parent: null
---

# Opt-in exact in-memory vector cache

## Commission

Authorized on 2026-11-19 by the operator, relayed through the authenticated factotum (18:52:25Z): approval to implement and release this capability so that a deployed consumer can integrate it, after the design-and-spike gate recorded in the [vector index cache](../../briefs/vector-index-cache.md) brief and the coordinator's GO for an exact-in-RAM, opt-in design and implementation phase. The operator's binding requirements for this capability, in generic terms: many concurrent queries against several memory-resident indexes that are updated over time; each query pins a stable set of index versions for its whole life while the next query may see newer ones; ordinary updates never duplicate an index in memory; the whole optimization is optional, with the exhaustive SQLite query remaining the default baseline needing no build, warm-up or optional dependency; a gain that cannot be told from machine noise is not worth adopting. Scope narrowed by the coordinator: the exact engine only; no memory-map setting, compact (1-bit) scan, graph index or default change; standalone Palace keeps the exact SQLite path as its default including the existing behaviour of a filtered search over chunks without vector rows. A deployed consumer may enable the cache in its own configuration; no consumer is named here. Plan review and code critique are by one independent cross-provider reviewer launched by the coordinator.

## Goal

A long-lived host process can opt in, per store, to serving the vector leg of search from a process-local in-memory copy of that store's vectors that returns the same results as the released exhaustive SQLite search, stays exactly current with every committed change, pins each query's view across every store it touches, never duplicates an index for an ordinary update, honours a memory budget and the caller's per-store authorization on every query, and reports plainly whenever it cannot serve. A store or process that does not opt in runs exactly the released code.

## Deliverables

- A per-store opt-in change feed: an operator command that enables or disables it, schema objects that exist only in enabled stores, every vector write site appending to it inside its own transaction, deletes clearing earlier feed vectors of the same chunk, bounded pruning, a lineage epoch, and a tripwire that detects a write made without the feed.
- An exact in-memory accelerator: append-only blocks with insert and delete stamps, immutable views, refresh to the querying snapshot, block-at-a-time compaction under an in-flight horizon, immediate eviction, a version-checked fast loader with a supported fallback route, and a cache with a byte budget, block reuse, single-flight bounded loads and an authorization hook.
- An explicit `vector_cache` argument on the single- and multi-store search entry points, per-store outcome reporting in their results, and an explicit unavailability setting (refuse, or answer from the exact SQLite path and report), never applied to an authorization or integrity refusal.
- Hermetic proofs for equality with the released path, visibility and coherence under concurrent writes, memory and queue bounds, restart, hybrid and reranked routes, the loader tripwire and authorization; an operations document; an Unreleased changelog entry; proof-estate admissions.

## Acceptance

- Every existing proof passes unchanged; a search without a cache imports no new module and a store without the feed is byte-for-byte unaffected by every writer.
- With the cache, vector, hybrid and reranked results equal the released path's on the synthetic fixtures, filters included, and the filtered missing-vector case returns what the released path returns.
- A commit injected between admission and retrieval, for one store and for the second of two stores, gives the released path's answer; a later query sees it; no probe across concurrent inserts, deletes and paired edits is stale or incoherent.
- No update allocates a second copy of an index; compaction's transient memory is at most one block; the budget bounds resident blocks; loads are single-flight and bounded, and excess is reported as unavailable.
- An unknown sqlite-vec version or a failed self-check uses the supported loader route and says so; a write that bypassed the feed is detected before any stale answer and refused; an authorization refusal fails the query and evicts.
- Focused tests and both full gates pass against their fixed candidates, with the independent plan review and code critique and their dispositions retained.
- Manual: whether the cache meets a deployed consumer's need, and its release, are the operator's; Linux runs only in hosted CI.

User Demo: N/A — library behaviour proved by deterministic synthetic tests; no live store or provider.

## Exclusions

- The memory-map read setting, the compact (1-bit) scan, graph indexes and any change to the default search path, including the filtered missing-vector behaviour, which wants its own fix.
- A background refresher, a disk-backed index, cross-process sharing, a native kernel for interpreter-lock scaling.
- Any consumer integration, release, tag or deployment.

## Brief refs

- [vector index cache](../../briefs/vector-index-cache.md)
- [deployed consumer readiness](../../briefs/deployed-consumer-readiness.md)
- [store parametric external consumers](../../briefs/store-parametric-external-consumers.md)
