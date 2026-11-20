---
title: A clean three-way merge can carry donor defaults into target-owned code
scope: methodology
proposed_surface: skill
source: teach
slug: talented-falcon
status: candidate
filed: '2026-11-20'
occurrences:
- date: '2026-11-20'
  ref: 'Starter v1.9.0 teach to palace, 2026-11-20: bin/kickoff-config shipped pins
    and workflow defaults'
---

As of 2026-11-20, `bin/kickoff-config` merged without conflict yet replaced palace's shipped quality-preset pins with Starter's lead-model pins, because the donor rewrote lines the target had never edited. The teach rules protect target-owned values in `kickoff.yaml`, but the same values also live as defaults in code. The behavioral suite caught it only because palace's seed fixture disagreed. After a merge, compare every target-owned default the code carries, not only the configuration file. This is an unratified candidate for `learn`.