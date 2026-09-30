---
title: "Memory and recall architecture"
date: 2026-09-30
status: draft
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Memory and recall architecture

## A.2 Consolidation

Cold-path extraction scores candidate facts and promotes only those meeting confidence, relevance and corroboration gates. Contradictions remain held for explicit adjudication; captures never perform extraction on the latency-sensitive path.

## D.2 Recall and continuity

Preserve provenance, event/ingest time and bounded startup context. Retrieve durable facts as needed rather than injecting an unbounded fact file into every session. Evaluation must distinguish whether relevant material was retrieved from whether a consuming agent used it.

## B.8 Privacy and on-device-first

Palace is a local-first memory substrate. Capture writes raw records without inference on the hot path. Cold paths consolidate durable facts and reconcile derived indexes. Provider choice is explicit, store-scoped and fail-closed; an endpoint's reachability is not authorization to send corpus material.

## C.1 Storage and hybrid retrieval

Textual source records and Markdown facts remain authoritative. SQLite indexes are derived. Lexical retrieval, dense retrieval and rank fusion address different query shapes. Reranking has explicit provisioning, failure status and cost boundaries; it must not be confused with relevance qualification.

## C.3 Graph-backed memory

Typed relations are a possible extension when a demonstrated workload requires graph traversal. They do not replace prose authority or justify inventing a fixed predicate vocabulary prematurely.

## C.4 Temporal / bitemporal: the underrated structural choice

Durable facts distinguish when a claim is valid from when it was observed. Preserve provenance and explicit invalidation/supersession rather than silently editing history.

## E.2 Workload-specific evaluation

Before tuning retrieval parameters, swapping models or changing expansion posture, qualify the change against a representative, consented workload. Synthetic fixtures are useful regression evidence but do not satisfy this relevance prerequisite. The shipped default-on reranker remains an explicitly documented evaluation debt: representative evaluation must confirm or reverse it. No private workload evaluation is claimed by this public source tree.

## G. Sequencing

Maintain store scoping, source provenance, inspectable derived indexes, strict provider boundaries and hermetic proofs before extending connectors, daemon platforms or graph features. New capabilities require concrete plans and acceptance evidence. No deployment or private integration history is part of this brief.
