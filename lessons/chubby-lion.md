---
slug: chubby-lion
title: Read the code at its definition before disposing a finding as unreachable
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 31 code critique CODE-F001: the local no-row evidence guard refused a single-store search that succeeds with evidence off"
---

As of 2026-11-12, Phase 31's first plan review (PLAN-F001) asked for a per-entry status when a hit has no chunk row. The revised plan answered it with a premise: a missing local row arises only for a remote-vector store. That premise is true of the multi-store seam, whose candidate hydration refuses such a hit, but `_fuse` in palace/search.py returns an orphan vector or keyword posting as a hit for one store. The premise was copied into a code comment and an invariant guard, the plan's second review did not reopen it, and the code critique found that evidence-on refused a call that succeeds with evidence off. Root then directed the closed status the first review had asked for.

When a finding is disposed as unreachable or out of scope, name the definition that makes it so and read that definition for every seam the disposition covers before relying on it, and add a proof that drives the supposedly unreachable input when one is cheap. A disposition that rests on an unread premise is still an open finding. This is an unratified candidate.
