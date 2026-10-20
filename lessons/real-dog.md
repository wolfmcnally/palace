---
slug: real-dog
title: Check the actual persistent identity key before selecting a fresh phase
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-10-20
source: kickoff
occurrences:
  - date: 2026-10-20
    ref: "Phase 21 identity correction: Phase 20 watcher refused before provider launch"
---

As of 2026-10-20, selecting the next phase from visible plan files and run metadata missed an existing advisory budget. The budget filenames are hashes of the phase string, so looking for a readable phase-number filename could not prove absence. The watcher correctly refused before launching the provider. Inspecting the defining budget_path function and checking its actual keyed records established that numeric identities through 20 were already used and 21 was genuinely unused.

Before declaring an identity unused, inspect its owning persistence/key function and every consuming lifecycle surface; a visible filename inventory is only a proxy for identity occupancy. Preserve denied attempts, original captures and used budgets. This incident was corrected with a new commissioned identity and fresh capture, not by resetting history. A bounded early supported identity-availability check could prevent this wasted initialization; it is a candidate methodology improvement, not authority to change the checker in this product phase.
