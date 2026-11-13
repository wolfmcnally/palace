---
slug: ultra-stallion
title: Search escaped and pattern forms of the old version when bumping a release
status: candidate
scope: local
proposed_surface: bin
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 25 ordinary 0.5.0 release follow-up"
  - date: 2026-11-12
    ref: "Phase 32 ordinary 0.5.1 release follow-up: the lesson's own example pattern 0[\\._-]?5[\\._-]?0 allows one separator character, so it missed the smoke test's two-character escaped form 0\\.5\\.0; caught by reading the test file, before any gate ran"
---

As of 2026-11-11, the 0.5.0 version bump searched the tree for the literal string 0.4.1 and recorded in a continuation checkpoint that the smoke test carried no version literal. The smoke test asserts the CLI output with the regular expression `^palace 0\.4\.1\s*$`, whose escaped dots a literal search cannot match. The first focused smoke run after the checkpoint failed on that assertion. The fix was one mechanical line, but the checkpoint had already stated a false fact about the tree.

When a release changes the version, search for the old version as a pattern that also matches escaped and separator variants (for example `0[\\._-]?4[\\._-]?1`), or let a small release-preparation check list every remaining occurrence outside history files. Do not record "no occurrence" from a literal search alone. This is an unratified candidate for a release-preparation helper.
