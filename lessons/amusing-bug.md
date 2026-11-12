---
slug: amusing-bug
title: Search every test inventory of a wrapper's command line before the full gate
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 30 acceptance attempt 1: ./bin/check all failed with 3 stale oracles in tests/test_toolchain_entrypoints.py"
  - date: 2026-11-12
    ref: "Phase 31 inventory grep found the stale leaf-ratio oracle in tests/test_test_governance.py before the full gate"
---

As of 2026-11-11, Phase 30 changed the exact pytest command line that `bin/test` emits. It updated the call-list oracles in tests/test_check.py, which the plan named, but tests/test_toolchain_entrypoints.py asserts the same uv call list independently. Focused checks ran only the named files, so the stale oracles surfaced first in the full gate, which cost one failed acceptance attempt, a direct fix and a full rerun.

When a phase changes a wrapper's emitted command line, grep the whole tests tree for the old call string, for example `pytest -q tests`, and add every hit's file to the focused set before critique. This is the repository-level form of the rule to update every independent fixture inventory of a contract. It is an unratified candidate.
