---
slug: dark-newt
title: Budget vector-store qualification by physical blocks rather than source size
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-04
source: kickoff
occurrences:
  - date: 2026-10-04
    ref: "Storage-error follow-up END (correction): vector allocation controls"
---

As of 2026-10-04, sqlite-vec 0.1.9 reserves 1024 vector slots by default. With 4096 float32 components, one synthetic vector occupies a 16 MiB vector blob. A delete/reinsert replacement wrote approximately 16 MiB to WAL. A held reader prevented checkpoint progress; closing the reader allowed truncation. A separate 8-slot control reduced the vector blob to 128 KiB and replacement WAL to about 157 KiB; the application schema was not changed.

Small source fixtures are an unreliable proxy for small storage demand. Before fixing a capacity budget, measure vector-block allocation, WAL bytes and physical free space at each acknowledged commit, and distinguish write amplification from reader-held reclamation. Size qualification fixtures using that evidence. A smaller chunk needs retrieval and throughput qualification before adoption; successful small controls do not settle that choice.

The default and allocation formula are explicit in [the pinned extension source](https://github.com/asg017/sqlite-vec/blob/v0.1.9/sqlite-vec.c), retrieved 2026-10-04. The synthetic control corroborates them empirically. This is one observation and a candidate lesson, not a graduated rule.
