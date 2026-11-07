---
slug: banana-kingfisher
title: The watcher's artifact flag is venue-specific; read its refusal before the first dispatch
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-07
source: kickoff
occurrences:
  - date: 2026-11-07
    ref: "Phase 22 planner attempt 1 and plan-review attempt 1: prelaunch watcher refusals"
---

`kickoff-config watch` takes the role artifact through `--result-file` for the Claude venue and through `--required-output-file` for the Codex venue, and refuses the other flag before launch. The kickoff dispatch resource names neither flag, so the first dispatch to each venue in Phase 22 was refused once and relaunched. Both refusals were prelaunch, consumed no budget and left no half-run, but each cost a round trip.

A one-line table in the dispatch resource, or a watcher that accepts either flag and routes it by venue, would remove the trip. This is one observation of each venue and a candidate lesson, not a skill change.
