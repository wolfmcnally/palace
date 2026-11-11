---
id: '25'
title: Prepared multi-store search
depends_on:
  - phase-24.md
informs: []
review_lane: full
---

# Prepared multi-store search

## Commission

Authorized on 2026-11-11 through the authenticated factotum, after a read-only producer assessment: split multi-store search into a preparation step that does all pre-provider admission and then the query expansion and embedding, and an execution step that freshly admits the current stores and retrieves with the prepared vectors only. This lets a consumer that fences its own view of the corpus capture that view after the slow, content-independent embedding, rather than across it. This is one coherent library outcome. Both review and evidence lanes are full. Independent same-provider advice is authorized while the other provider is constrained. Ordinary guarded commit and fast-forward push are authorized. No release, tag, artifact publication, dependency-pin change, policy or configuration change, permission change or user-data operation is authorized.

The factotum fixed this scope: full pre-provider request, store, identity and boundary admission, then fenced query expansion and embedding; immutable value-only prepared state with no surviving connection; fresh execution admission and current corpus snapshot, bound to stable query, provider, store and backend identity, never to an old corpus revision, checkpoint or watch-root set. No execution-time embedding, hidden retries, weakened refusals or provider-call changes. Retrieval, hydration, union rerank and strictness stay as they are, and the existing `search_stores` becomes exactly the composition of the two steps. No snapshot body or hash API and no rerank-outside-the-fence design. Execution against a store whose vector leg is a published S3 Vectors index still makes that provider request.

The operator also directed on 2026-11-11 that mirror content supplied to an index is user-owned: users may edit it, and the index must re-index it rather than treat the change as corruption or regenerate it. Palace never regenerates source files. In this phase, a corpus change between preparation and execution is adopted by execution's fresh snapshot.

## Goal

Let a caller prepare a multi-store query, which is validated, expanded and embedded, before entering its own consistency window, and then execute it against the stores' current content with the same results, refusals and provider calls as the existing single call.

## Deliverables

- A public `prepare_store_search` returning an immutable, process-local `PreparedStoreSearch` value, and a public `execute_prepared` that freshly admits each store, refuses any change to the prepared stable bindings, and retrieves, hydrates and reranks with the prepared expansion outputs and vectors only.
- `search_stores` reimplemented as exactly that composition, with unchanged signature, results, refusal texts, provider-call counts and inputs, resource ownership and strict semantics.
- Hermetic synthetic proofs inside the existing multi-store proof families: composition equivalence, adoption of corpus changes between the steps, refusal of each stable-binding change, refusal of an execute-time embed or expansion, and integrity refusal of an altered or foreign prepared value. Their contract text is updated in the proof estate at zero family and leaf growth.
- Operator-facing documentation in the multi-store guide and an Unreleased changelog entry.

## Acceptance

- `search_stores` results equal those of `prepare_store_search` followed by `execute_prepared` on the same inputs, across bm25, vector and hybrid modes, HyDE and multi-query expansion, mixed identities, injected and constructed providers, and rerank applied, disabled and failed. Injected embedder, expander and reranker call counts and inputs are unchanged from the current single call.
- Rows added to or removed from a store between preparation and execution appear in or vanish from the result. Execution never compares row counts, generation, receipt checkpoint or the watch-root set; it re-runs the remote-corpus boundary on the current watch roots.
- Execution refuses before any vector query or rerank when a store's embedding identity, embedder selector, vector-backend selection or stable published-index identity, or reranker selection, has changed; or when a store's database is missing or its identity unreadable; or when the boundary now fails. Execution never constructs an embedder or expander, and any input not prepared refuses instead of embedding.
- A prepared value whose fields were altered, or one not produced by `prepare_store_search` in this process, refuses before any store is opened. Preparation holds no connection, snapshot or constructed provider after it returns, on success or failure; injected providers stay caller-owned.
- Focused tests and both complete repository gates pass against their respective fixed candidates, with independent plan and code advice and primary dispositions retained.
- Manual: review the clarity of the two-step API and its documented guarantees. Automated evidence does not settle this judgment.

User Demo: N/A — this is a library composition seam with deterministic synthetic proofs, without a new interactive surface.

## Exclusions

No consumer-repository edit, release, tag, pin, publication, live provider or paid call, new provider or model, CLI flag, persistence or cross-process transfer of prepared values, snapshot body or hash return, rerank outside the consumer's window, retry, or change to single-store search, defaults, pool sizes, fusion or reranking. Faster execution is claimed only for stores whose vector leg is local sqlite-vec.

## Brief refs

- [External-consumer readiness](../briefs/deployed-consumer-readiness.md)
- [Embedding identity](../briefs/embedding-provider-and-index-identity.md)
