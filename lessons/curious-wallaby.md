---
slug: curious-wallaby
title: Pin a dependency's API source before a plan cites it
status: candidate
scope: methodology
proposed_surface: policy
filed: 2026-11-07
source: kickoff
occurrences:
  - date: 2026-11-07
    ref: "Phase 22 plan artifact 1: concreteness checker refused nine Conric definitions read from a sibling checkout"
---

A plan that cites a new dependency's API must name an in-tree definition for every identifier, and the dependency is not installed until the plan's first implementation step. The planner cited the sibling checkout, which the concreteness checker correctly refused. Pinning the dependency's public API module at the exact targeted commit under `docs/reference/` (blob-hash verified, cataloged with its license) made the citations checkable and gave the coder and critic a network-free authority; the pinned commit happened to publish no manual, so its typed source module was the right pin.

When a phase adds or upgrades a dependency, land the pin before planning, in the same pre-run step that writes the brief. This is one observation and a candidate lesson, not a policy change.
