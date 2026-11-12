---
id: '31'
title: Returned-hit evidence with prepared single- and multi-store search
depends_on:
  - phase-25.md
informs: []
review_lane: full
---

# Returned-hit evidence with prepared single- and multi-store search

## Commission

Authorized on 2026-11-11 by the operator's root, relayed through the authenticated factotum: give a consumer caller-verifiable, exact evidence for each hit a search returns, and let the consumer own whatever policy it applies to that evidence. Palace adds no privacy, retention or access policy of its own at this level. The binding constraints are:
- search inputs, the number of hits and their order stay unchanged;
- each returned hit's body and stored vector are read and checked in the same SQLite read transaction the search used, at O(d) per hit;
- a damaged entry is attributed and isolated, not allowed to fail or reshape the whole result;
- the residual limits for omission, keyword postings, ranking and remote stores are stated explicitly;
- no remote provider call is introduced inside the caller's consistency window, and no snapshot equivalence across stores is implied.

Concrete signatures are coordinated with the consumer through the coordinator after plan capture. The coordinator's interface check showed that the consumer also uses single-store retrieval, with and without reranking, and needs its provider work outside its own consistency window. A one-store multi-store call does not preserve the single-store contracts, so root authorized the minimum necessary single-store prepare-and-execute seam in this phase, with no speculative command-line or strict surface. This phase is independent of Phases 26 to 30 and changes no test lane, gate, policy or native watcher proof. The working tree carries those phases' uncommitted changes and an uncommitted proof-manager methodology correction; this phase does not claim, accept or reinterpret any of them, and in `tests/proof-estate.yaml` and the governance ledger it owns only its own entries and its own share of the budget fields. Both review and evidence lanes are full.

## Goal

A caller can prepare a single-store or multi-store search outside its own consistency window, execute it against the current rows, and ask for per-hit evidence. For every returned hit, in its original order, it receives either the exact stored body, vector and identifying fields verified within that store's retrieval snapshot, or a closed status naming why that one entry could not be verified.

## Deliverables

- A prepared single-store seam: preparation does every pre-provider admission and then the query expansion and embedding, and execution freshly admits the store and retrieves with the prepared inputs only, with or without reranking. The existing single-store search entry points become exactly that composition, with unchanged inputs, batching, candidates, order, hits, scores, refusals and strict posture.
- An optional evidence flag, bound at preparation, on both the single-store and multi-store prepared searches. With it off, results, refusals and provider calls are unchanged. With it on, the same hits are returned in the same order with the same scores, plus one evidence entry per hit.
- One immutable per-hit evidence value for both seams. It carries the hit's store, watch root, path, file hash, chunk identifier, section and window position, recorded embedding identity and a closed status. Only a verified entry also carries the exact stored body, its recorded digest, its embedding-input digest and the exact stored vector bytes.
- Per-entry statuses for a body whose digest differs from the recorded one, a missing vector row, a malformed vector, a missing embedding-input digest, and a hit from a store whose vector leg is remote.
- Documentation of the guarantees and residual limits in the multi-store guide, and an Unreleased changelog entry.
- Proof-estate admissions for the new proofs under a named, exactly sized positive budget.

## Acceptance

- Every existing single-store and multi-store proof passes unchanged, and each single-store entry point equals the preparation followed by execution on the same inputs, with the same provider calls and inputs.
- Execution never embeds or expands, searches the current rows, and refuses a changed recorded identity, embedder, vector backend, published index or reranker selection before any vector query, rerank or provider call.
- With evidence on, hits, order, scores, status and provider-call counts equal the evidence-off call on every return path, and each verified entry's fields equal the rows read in the retrieval's own snapshot. A writer commit during the search does not change the evidence. Evidence with keyword-only search refuses before any provider call.
- One damaged entry per status among healthy hits yields that status on that entry only, with every other entry verified and the hit list unchanged.
- No new remote provider request is made by evidence collection; a remote-vector store's hits are reported as remote-only.
- Focused tests and both full gates pass against their fixed candidates, with one independent plan review and one code critique and primary dispositions retained.
- Manual: whether the evidence contract and its stated limits meet the consumer's need belongs to the operator and the consumer.

User Demo: N/A — a library seam proved by deterministic synthetic tests.

## Exclusions

- Any privacy, retention, redaction or access policy.
- Command-line evidence output and a strict evidence mode; the consumer uses the library and report mode only.
- Detecting omitted or unreturned chunks, keyword-index tampering, or ranking influence by a damaged vector.
- Cross-store snapshot atomicity, remote-store verification, and any change to retrieval, fusion, hydration or reranking.
- Test lanes, gates, policies, the native watcher proofs and the release.

## Brief refs

- [deployed consumer readiness](../briefs/deployed-consumer-readiness.md)
- [store parametric external consumers](../briefs/store-parametric-external-consumers.md)
