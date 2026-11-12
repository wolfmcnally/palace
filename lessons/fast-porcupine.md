---
slug: fast-porcupine
title: Size an external reviewer's reading list to its turn cap
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 30 plan review attempt 1: Claude reviewer reached the 50-turn cap at 471 s with no findings or verdict"
---

As of 2026-11-11, the Phase 30 plan-review prompt named about sixteen repository files and two out-of-tree design documents, plus six open-ended examination questions, for a Claude reviewer whose configured circuit breaker is 50 turns. The reviewer spent its turns reading one file per call and stopped at turn 51 with a progress note as its only result, so the phase's single review produced nothing. The planning resource already says to scope the reading mandate to the handful of load-bearing files; the prompt did not.

Before dispatching a Claude role, count the files it must open against the role's turn cap and leave most of the cap for analysis and the report: name only the files the plan reshapes, quote short definitions inline, and point to the rest as optional. This is an unratified candidate.
