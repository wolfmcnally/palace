---
slug: rousing-foxhound
title: Count non-terminal attempts when a release rule names the most recent terminal run
status: candidate
scope: local
proposed_surface: bin
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 30 code critique CODE-F001"
---

As of 2026-11-11, the root ruling on failed native runs said release needs the most recent terminal native run for the exact candidate and host to be the passing one. The first implementation filtered out records still marked running, reading the rule literally. An interrupted run, or a passed run whose completion refused on drift, therefore stayed running forever and vanished from the verdict: a pass followed by such an attempt verified with zero failures reported. The independent critique caught it.

When evidence is selected by status, ask what an attempt that never reached a terminal status means for the verdict before filtering it out. Count unfinished attempts as non-passing, refuse when one is newer than the qualifying pass, and make every refusal path write a terminal status before it raises. It is an unratified candidate.
