---
slug: rough-ringtail
title: Edit appended JSONL rows by line, never by re-serializing the whole file
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 31 post-critique ledger edit: a json round-trip re-serialized six committed rows of reports/test-governance/palace-reset.jsonl"
---

As of 2026-11-12, a Phase 31 correction to its own admission rows parsed and rewrote the whole governance ledger. Six committed rows had been written with compact separators, so the round-trip changed their bytes although their values were unchanged, which silently altered other owners' history in a shared, append-only file. It was caught by reading the diff hunks against HEAD and restored byte-exact; the pre-phase prefix hash matched the continuation packet.

When correcting rows a phase appended to a shared JSONL ledger, rewrite only those lines and assert afterwards that the prefix before them is byte-identical to its prior hash. Never load and re-dump the whole file. This is an unratified candidate.
