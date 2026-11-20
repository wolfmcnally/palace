---
title: The converter drops handwritten prose inside the phase-table section
scope: methodology
proposed_surface: bin
source: teach
slug: crouching-leopard
status: candidate
filed: '2026-11-20'
occurrences:
- date: '2026-11-20'
  ref: 'Starter v1.9.0 teach to palace, 2026-11-20: plan/INDEX.md lost its identity-correction
    paragraphs in conversion'
---

As of 2026-11-20, the converter removes the whole `## Phase Table` section from the plan index, including any paragraphs written under the table. Palace kept its private-history note and both dated identity corrections there; they are the only provenance for its retired phase sketches. They were restored verbatim by hand after the byte checks had already passed, because nothing compared retained prose. Conversion should keep non-table prose from that section, or at least report what it dropped. This is an unratified candidate for `learn`.