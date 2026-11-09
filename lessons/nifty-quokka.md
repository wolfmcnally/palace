---
slug: nifty-quokka
title: Make refused replacement fixtures observably different from stored state
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-09
source: kickoff
occurrences:
  - date: 2026-11-09
    ref: "Phase 24 snapshot-budget correction code-2 CODE-F001"
---

As of 2026-11-09, independent code advice identified that the replacement rollback fixture reused the same body, vectors and fixed ingestion time as the existing file. Comparing all stored rows before and after refusal could therefore pass even if a byte-identical replacement had been published. The insertion witness still detected publication before refusal; this was a narrower proof gap, not an established implementation defect.

Use a replacement whose persisted values and identities differ, then check both the relevant tables and the prior public snapshot after refusal. The corrected fixture changes the body, exact input and chunk identity. A disposable control that commits this replacement before the capacity check fails the state assertion, demonstrating that the corrected witness distinguishes refusal with rollback from publication followed by refusal. Keep fault controls synthetic and preserve the unmodified implementation; passing without a fault does not by itself establish the strength of the refusal proof.
