---
slug: towering-dormouse
title: A NULL KNN distance fails retrieval with an unnamed TypeError
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 31 malformed-vector proof: a NaN stored vector reached by the vector query raised TypeError from float(None) in _vector_search"
---

As of 2026-11-12, sqlite-vec returns a NULL distance for a stored vector holding NaN when the KNN query reaches it, and the existing `_vector_search` in palace/search.py converts it with `float(dist)`. The search then fails with a `TypeError` instead of the project's named `IndexError`, before any result or evidence exists. An infinite component returns `inf` and searches normally. Phase 31 recorded the limit in docs/multi-store.md and kept a reproduction, but its exclusions forbade retrieval changes.

A later phase that may change retrieval should turn a NULL distance into a named `IndexError` naming the store and chunk, with a proof that drives a NaN vector. This is an unratified candidate.
