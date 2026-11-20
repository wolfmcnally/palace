---
title: The layout converter cannot express named, retired or file-referenced phase
  records
scope: methodology
proposed_surface: bin
source: teach
slug: resolute-wolverine
status: candidate
filed: '2026-11-20'
occurrences:
- date: '2026-11-20'
  ref: 'Starter v1.9.0 teach to palace, 2026-11-20: transfer-preflight refused with
    ''missing phase table'''
---

As of 2026-11-20, the v1.9.0 `multioperator` converter accepted only `## Phase Table` (exact case), numeric `[Phase N](phase-N.md)` rows, phase ids in `depends_on`, and a phase-file set equal to the rows. Palace's legitimate records had a lowercase heading, a named completed `Release` row, two retired sketches kept on purpose outside the table, and dependencies written as file names. Every variant refused, and the only automated workaround would have renumbered history or deleted the retired files.

The teach completed by a recorded manual transcription that called the converter's own functions, read the named row and dependency aliases explicitly, and kept the retired sketches byte-identical with a reserved-identity record. A converter that accepts case-insensitive headings, named rows, file-name dependency aliases and an explicit retained-unimported list with digests would make this route supported. This is an unratified candidate for `learn`.