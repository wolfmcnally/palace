---
slug: dazzling-wallaby
title: Read a lane wrapper's argument contract before composing a gate argv
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 26 resumed acceptance attempt 1: ./bin/test with explicit paths forwarded them to pytest unchanged, so the native watcher lane ran inside an ordinary focused gate without a coordinator slot"
---

As of 2026-11-12, `./bin/test` applies the ordinary-lane marker `-m "not native"` only when it receives no arguments or runs a governed lane (`--vital`, `--changed-from`); any other arguments are forwarded to pytest unchanged, as its usage text says. Phase 26's resumed acceptance manifest named `tests/reindex` explicitly, on the mistaken belief that the governed lane's empty-native-selection rule applied, so the ten native watcher proofs ran in the ordinary focused gate outside any coordinator-confirmed slot. One failed late, the attempt was recorded as failed, and a successor manifest with an explicit `-m "not native"` selection replaced it.

Before writing a gate manifest, read the wrapper's `--help` for how it treats forwarded arguments, then run the exact argv with `--collect-only` and check the collected and deselected counts against the intended lane. This is an unratified candidate.
