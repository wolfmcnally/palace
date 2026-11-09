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
---

As of 2026-11-09, finalizing a failed follow-up appended EXECUTION_LOG.jsonl while an existing report still reflected the earlier trace set. The next complete gate passed all 268 behavioral tests, then correctly refused stale dashboard bytes. The report checker regenerates from every finalized phase trace, so retaining the earlier accepted trace does not freeze the report's source bundle.

After failed-trace finalization, refresh an existing phase report using its real historical accepted trace and unchanged handoff before another full gate. Preserve failed outcomes and do not mark the failed trace accepted. Run the inexpensive dashboard checker before repeating the full suite. Capture the regenerated candidate normally; generated reports are still candidate content. This is a proposed workflow lesson, not authority to relax checks or rewrite historical evidence.
