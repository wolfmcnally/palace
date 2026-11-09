---
id: '24'
title: Compatible file-vector reuse
depends_on:
  - phase-23.md
informs: []
review_lane: full
---

# Compatible file-vector reuse

## Commission

Authorized on 2026-11-08 through the authenticated factotum: close two generic library seams for same-store body-vector reuse, with independent destination filename embeddings and destination-owned search rows. This is one coherent library outcome, using the existing per-file planning and atomic commit operation. Both review and evidence lanes are full. Independent same-model advice is authorized while the other provider is constrained. Ordinary guarded commit and fast-forward push are authorized; no release or tag publication is authorized.

The operator explicitly requires: “Supported admission and verification of the consumer's independently compatible body/name embedding representation. Current hard-coded breadcrumb-path-heading-v1 prepends destination-specific path/heading, so equal body bytes do not alone justify vector reuse. Preserve truthful representation identity; do not silently relabel old vectors or globally change existing default semantics just to make reuse appear valid.” This scope permits explicit alternate representation admission; the existing breadcrumb construction and default entry points retain their semantics.

## Goal

Let a caller read one nominated file's committed chunks, vectors and matching file/index identity, then reuse only exactly compatible body inputs while independently embedding each destination's name and publishing destination-owned rows atomically.

## Deliverables

- A supported bounded nominated-file reader returning committed ordered chunks/vectors and matching file/index identity observations, without exposing arbitrary SQL or private tables as the caller API.
- Explicit body/name representation admission and verification that binds exact ordered inputs/digests, provider, model, dimension, implementation, chunker, name policy and actual view, preserving existing defaults and refusing incompatible stored identities.
- Documented composition through the existing FilePlan and commit_plan transaction under one same-store writer lock acquired before connection and donor read and held through publication and connection close.
- Hermetic synthetic proofs of reuse, independently generated destination identity/name rows, identity/input/view mismatches, bounded read refusal, missing versus corrupt donor behavior and atomic failure; operator-facing API documentation and changelog notes.

## Acceptance

- Same-body files at different destination paths reuse compatible body vectors with distinct destination chunk identities and independently embedded filename rows; donor removal leaves destination rows searchable.
- Differences in any admitted identity axis, ordered exact input, name policy or actual view refuse reuse; a missing nominated file is explicit and permits ordinary ingestion, while malformed committed data refuses without writes.
- Reader limits are enforced before unbounded materialization and the reader returns only the nominated watch-root/path. Lock lifetime and same-thread SQLite ownership are explicit; no nested lock-taking writer is introduced.
- Existing breadcrumb plans and normal build/search defaults remain unchanged; alternate identities are never silently stamped onto populated breadcrumb vectors.
- Focused tests and both complete repository gates pass against their respective fixed candidates, with independent plan/code advice and primary dispositions retained.
- Manual: review API clarity and the documented caller responsibilities. Automated evidence does not settle this judgment.

User Demo: N/A — this is a library composition seam with deterministic synthetic proofs, without a new interactive surface.

## Exclusions

No transaction/cache/index engine, arbitrary SQL API, consumer eligibility or security policy, new provider/model, MCP, cloud resource, live personal corpus, credential/permission change, other repository writes, release/tag publication or history rewrite. Reuse does not claim provider determinism or retrieval-quality improvement.

## Brief refs

- [Embedding identity](../briefs/embedding-provider-and-index-identity.md)
- [External-consumer readiness](../briefs/deployed-consumer-readiness.md)
