---
slug: ethereal-weasel
title: Refresh existing reports after failed-trace finalization before the next gate
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-09
source: kickoff
occurrences:
  - date: 2026-11-09
    ref: "Phase 24 snapshot-budget correction dashboard PARK"
  - date: 2026-11-11
    ref: "Phase 22 bearer correction handoff-gate rerun: earlier Phase 22 report stale after a new accepted trace"
  - date: 2026-11-11
    ref: "Phase 29 focused policy check: stale dashboard artifact reports/execution/index-data.js; Phase 25 report not refreshed after a later failed Phase 25 trace was finalized"
  - date: 2026-11-12
    ref: "Palace 0.5.0 release close: the earlier-dated Phase 25 report went stale when the new accepted trace was archived under a new date; refreshed with its own accepted trace and a later phase re-render"
  - date: 2026-11-12
    ref: "Phase 25 dogfood fresh run, attempt 1: the policy gate refused stale dashboards after the superseded run's failed close appended a Phase 25 bundle"
---

As of 2026-11-09, finalizing a failed follow-up appended EXECUTION_LOG.jsonl while an existing report still reflected the earlier trace set. The next complete gate passed all 268 behavioral tests, then correctly refused stale dashboard bytes. The report checker regenerates from every finalized phase trace, so retaining the earlier accepted trace does not freeze the report's source bundle.

After failed-trace finalization, refresh an existing phase report using its real historical accepted trace and unchanged handoff before another full gate. Preserve failed outcomes and do not mark the failed trace accepted. Run the inexpensive dashboard checker before repeating the full suite. Capture the regenerated candidate normally; generated reports are still candidate content. This is a proposed workflow lesson, not authority to relax checks or rewrite historical evidence.

As of 2026-11-11 the same generator recurred with an accepted trace rather than a failed one. A Phase 22 correction's accepted trace was archived as a new dated report; the existing 2026-11-07 Phase 22 report, whose source bundle covers every finalized Phase 22 trace, was not refreshed, and the bare handoff gate refused it after 268 passing tests. In a scratch copy, regenerating the old report from its own historical accepted trace and unchanged handoff and then regenerating the new report made the dashboard checker pass, changing only the old report's data file. Any new finalized trace for a phase with an existing report needs that refresh and the cheap dashboard check before the handoff gate.

As of 2026-11-11 the same refusal appeared in a later phase's focused policy check. A failed follow-up trace for already-delivered Phase 25 was finalized into EXECUTION_LOG.jsonl after Phase 25's report was generated, so the regenerated index counts two Phase 25 traces, one failed, against the committed one; the parked Phases 26 to 29 have no reports and do not affect the index. The primary first attributed the staleness to those parks without checking, and corrected it after regenerating the index in a scratch directory and diffing its fields. Regenerating the Phase 25 report from its own accepted trace and handoff into a scratch copy reproduced the checker's expected digest and changed only that report's data file and the index.

As of 2026-11-12 the generator recurred twice in one phase: once after an accepted trace was archived under a new date, and once after a failed close. Both were caught by the dashboard check and fixed by re-rendering every archived report of the phase. A later phase was then re-rendered so that the index was rebuilt from every archived payload.
