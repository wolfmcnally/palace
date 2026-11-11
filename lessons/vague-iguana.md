---
slug: vague-iguana
title: Enumerate failure-path call traces when a phase splits a call into stages
status: candidate
scope: methodology
proposed_surface: agent
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 25 prepared multi-store search"
---

As of 2026-11-11, Phase 25 split multi-store search into a preparation step (expansion and embedding) and an execution step (retrieval and rerank), under a commission promising "no provider-call changes". The plan compared only success traces. The independent plan review found that a BM25 refusal which used to stop before any embedding would now follow one. The first code critique found that deterministic vector-leg bounds (S3 top-K, receipt dimension, sqlite-vec k) would now refuse only after every paid embedding. It also found that failures occurring only during retrieval would follow embeddings the single call had skipped. The coordinator then had to reconcile the commission's literal wording against differences the split made unavoidable.

When a plan reorders or stages provider calls, write out the failure-path call trace for each refusal point before review: what the old code had already sent, and what the new code will have sent. Keep every deterministic request bound in admission ahead of paid work. State any unavoidable failure-path difference in the plan, so the commission can be qualified before implementation rather than after critique. This is an unratified candidate for the planner and plan-reviewer definitions.
