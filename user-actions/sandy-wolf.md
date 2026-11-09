---
slug: sandy-wolf
title: Review file-vector composition API clarity
status: pending
category: acceptance
urgency: low
blocks: []
filed: 2026-11-09
needed_at: "After Phase 24 delivery"
source: kickoff
refs:
  - palace/index/README.md
  - plan/phase-24.md
---

Review the caller-supplied embedding inputs section in palace/index/README.md. Confirm that the public reader and representation composition contract make it clear who chooses body/name rows, compares exact ordered model inputs, always embeds destination names, selects eligible donors and holds the writer lock through connection close. Confirm that existing breadcrumb vectors cannot be relabeled for reuse and incompatible derived state needs an explicit rebuild.

Automated tests establish storage behavior, bounds and refusals; they cannot settle whether this explanation is clear enough for callers. This judgment does not block guarded delivery. Return acceptance or concrete wording/API feedback; no live corpus, model service or destructive operation is needed.
