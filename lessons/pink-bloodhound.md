---
slug: pink-bloodhound
title: Run mutation checks on a copy, never in place in a checkout another writer shares
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-17
source: kickoff
occurrences:
  - date: 2026-11-17
    ref: "Phase 33: the core lane mutated palace/index/pacing.py in place for about two minutes while the adapter lane could run tests against the same tree; restored byte-identical and the peer was told to rerun anything that failed in the window; later red checks ran in a scratch copy with PYTHONPATH"
---

As of 2026-11-17, two writers shared one checkout with disjoint files. A mutation loop that rewrote a source file in place and restored it in a finally block was safe for the writer running it, but any test the other writer ran meanwhile imported a deliberately broken module and could have reported a false failure or, worse, a false pass of a differently broken tree. The fix that worked: copy the package and tests into a scratch directory, swap the mutant there, and run the repository interpreter with that directory first on PYTHONPATH; or mutate in-process from a pytest plugin, as the other writer did. This is an unratified candidate.
