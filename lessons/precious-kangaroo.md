---
slug: precious-kangaroo
title: Re-read the stage recipe before re-executing a procedure after compaction
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 26 implementation gate: run-gate refused before tests because orchestration.implementation was still open"
  - date: 2026-11-11
    ref: "Phase 26 revised-probe fault controls: the handler-suppression control was re-run against a proof it does not affect"
  - date: 2026-11-12
    ref: "Phase 27 design close: the open decision interval was closed after the resumed run's gates instead of before resuming phase work, because the recovery resource's park-close step was not re-read at resume"
---

As of 2026-11-11, after a context compaction, the primary reconstructed the acceptance sequence from memory and from earlier command transcripts instead of reading the acceptance stage resource. The first gate dispatch was refused before any test ran with "missing successful pre-acceptance orchestration stages: orchestration.implementation": the implementation stage had not been closed, the acceptance stage had not been opened and command zero had not run. In the same continuation, a scratch fault control that suppresses only one proof's target was re-run against a different proof, which passed; the logged recipe named the intended proof. Both were caught by a fail-closed boundary or an unexpected result and corrected without lost evidence, but each cost a round-trip inside a time-boxed shared test slot.

Before re-executing any multi-step procedure on a continuation, read its owning stage resource and the recorded recipe of the earlier run in full, and reuse the recorded command exactly rather than a remembered shape. A pass from a control that was expected to fail is a targeting finding to investigate, never a result. This is an unratified candidate.
