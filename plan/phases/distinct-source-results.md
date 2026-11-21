---
id: distinct-source-results
title: Opt-in distinct results per caller-supplied key
depends_on:
- release-zero-seven
informs: []
review_lane: full
order: '18'
parent: null
---

# Opt-in distinct results per caller-supplied key

## Commission

Authorized on 2026-11-21 by the operator, relayed through the authenticated factotum (06:28:22Z), as the first step of a distinct-source search tranche: a Palace enhancement and minor release that a downstream consumer then integrates. The operator's binding requirements, in generic terms: an opt-in grouping of eligible, already-ranked chunks that keeps the first (highest-ranked) hit per caller-supplied key; the distinct-result limit applies after eligibility and grouping; an ineligible stale head hit must not displace a valid lower-ranked sibling; determinism and identities are preserved across stores; the default chunk API is unchanged; exactly one rerank, no extra provider calls and no hidden growth of the candidate budget, with default and existing rerank admission behaviour unchanged; no consumer dependency or domain policy in Palace. The interface sketch was sent to the coordinator before planning for the consumer's input. No consumer is named here.

## Goal

A caller of single- or multi-store prepared search can ask for at most N distinct results by its own key and eligibility rule, taken in the search's existing final order from the hits the search already produced, with an explicit report of how many pooled hits were walked, how many were ineligible or duplicates, and whether the result is underfilled. A caller that does not opt in gets exactly the released results.

## Decisions pinned by this phase

- The grouping is an execution-time argument, `distinct=DistinctResults(key=..., limit=...)`, on `execute_prepared_search` and `multistore.execute_prepared`; prepared values stay sealed and free of caller objects. Without it every route returns exactly the released result.
- Four quantities stay separate and none changes: the retrieval pools and fused input budget (`pool`, `rerank_in`), the provider calls (one embedding replay, at most one rerank call over the admitted candidates), the ordinary ranked output cutoff (the prepared `limit`), and the requested distinct count (`distinct.limit`).
- Every supported reranker (the local cross-encoder, the private endpoint and an injected one) scores all admitted candidates in its one call and Palace truncates locally. On the reranked route distinct mode therefore walks the complete ranked pool of admitted candidates from that same call instead of its first `limit`: more ranked output from the same input, with no extra call or candidate. Single store: the `rerank_in` fused ids; multi-store: the union of each store's `rerank_in`. Without a successful rerank (failed without `strict`, disabled, direct or fused routes) the pool is what retrieval already produced, in fused order: the prepared `limit` on single-store direct and fused routes, `rerank_in` on a single-store failed rerank, the union on multi-store. Nothing is re-queried or widened.
- With evidence prepared, each pool hit's `HitEvidence` is read in its store's retrieval snapshot. Single store already covers every fused id; multi-store extends its in-snapshot read from the returned hits to the pool. The `key` callable runs after every snapshot and any vector-cache lease have closed, so caller checks never run under a Palace snapshot or use its connection.
- `key(hit, evidence)` receives the `SearchHit` (or `StoreHit` for multi-store) and its evidence or `None`, and returns a string group key or `None` for ineligible. It is called once per pool hit in pool order until `distinct.limit` hits are kept. Eligibility is decided before grouping and after the rerank: an ineligible hit is dropped and never heads a group, so the best-ranked eligible hit of each key is kept, by reranked order where a rerank applied. Hits keep their original rank and score fields; evidence stays aligned.
- `DistinctOutcome(pool, walked, ineligible, duplicates, returned, underfilled)` reports exact counts over walked hits; `underfilled` means fewer than `distinct.limit` were kept, after walking the whole pool. `distinct.limit` must be positive. A `key` exception propagates and no partial result is returned.

None of these departs from the commission; the public signature is an engineering choice within it, sent to the coordinator for the consumer's preparation before plan review.

## Deliverables

- `DistinctResults` and `DistinctOutcome` values and the `distinct=` keyword on both execution entry points, with a `distinct` field on `RerankedSearch` and `MultiStoreSearch` that is `None` when not requested.
- Hermetic proofs: a request for 10 with three early duplicates and later eligible groups in the existing reranked pool returns 10; a pool that genuinely lacks 10 distinct eligible keys returns fewer and reports underfilled; an ineligible high-ranked stale hit does not displace its eligible sibling, and the kept sibling is the reranked winner, not the fused one; cross-store identity carried by the caller's key; ties keeping the existing order; the reranked, failed-rerank, fused and direct routes; evidence alignment; byte-identical outputs without `distinct`; identical embedding, expansion and reranker calls and reranker inputs with and without `distinct`.
- An operations note in the search documentation, an Unreleased changelog entry and proof-estate admissions.

## Acceptance

- Every existing proof passes unchanged; without `distinct` every route's output equals the released output.
- With `distinct`, the returned hits are exactly the first eligible hit per key in pool order over the stated pool, at most `distinct.limit` of them, with evidence aligned and the outcome counts exact; an ineligible head never displaces its eligible sibling.
- Embedding, expansion and reranker calls and reranker candidate counts are identical with and without `distinct`.
- Focused tests and both full gates pass against their fixed candidates, with the independent plan review and code critique and their dispositions retained.
- Manual: whether the interface meets the consumer's need, and the release, are the operator's.

User Demo: N/A — library behaviour proved by deterministic synthetic tests; no live store or provider.

## Exclusions

- Widening the candidate pool, a second rerank, or any change to default limits, pools or rerank admission.
- Domain-specific keys or eligibility rules, a CLI flag, or any consumer integration.
- The release itself (a separate release step) and the remaining query-time SQLite reads of the vector cache.

## Brief refs

- [store parametric external consumers](../../briefs/store-parametric-external-consumers.md)
- [deployed consumer readiness](../../briefs/deployed-consumer-readiness.md)
