---
slug: jade-hare
title: A delegated coder venue cannot run the whole suite green while the delegation guard is set
status: candidate
scope: methodology
proposed_surface: test
filed: 2026-11-07
source: kickoff
occurrences:
  - date: 2026-11-07
    ref: "Phase 22 coder report: full ./bin/test inside the delegated session failed one kickoff-evidence proof on KICKOFF_DELEGATION_DEPTH"
---

The watcher sets `KICKOFF_DELEGATION_DEPTH` in every delegated role's environment so a role cannot delegate again. One kickoff-evidence proof exercises delegation and therefore refuses under that variable, so a delegated coder that runs the whole suite sees one environmental failure that the orchestrator's own full gate does not. The Phase 22 coder reported it truthfully and the orchestrator's candidate gate passed all 268.

Either the proof should skip or isolate the variable when it is set by an enclosing run, or the coder role should be told that the whole-suite run belongs to the orchestrator. One observation; a candidate lesson, not a test change.
