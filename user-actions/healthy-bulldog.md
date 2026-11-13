---
slug: healthy-bulldog
title: Decide when the four skipped native watcher proofs return to release qualification
status: deferred
category: decision
urgency: low
blocks:
  - Full native watcher coverage in release qualification
filed: 2026-11-12
needed_at: "After fseventsd on the release host is healthy (fair-chimera)"
source: kickoff
refs:
  - tests/reindex/test_server.py
  - tests/proof-estate.yaml
  - LOG.md
---

This is not done. By the operator's ruling of 2026-11-12, recorded verbatim in the Phase 26 END (correction) in LOG.md (execution trace d700d23090d24589b73c0f6f433a958a), four native watcher proofs carry an explicit skip for release qualification: test_serve_emits_jsonl_line_within_two_seconds_of_touch, test_serve_skips_missing_on_disk_root, test_serve_recovers_from_one_observer_callback_exception and test_serve_sigterm_drains_inflight_debouncer. On the release host, each one's post-readiness write was not delivered by FSEvents within the 55-second delivery hang bound. The proofs stay in the estate, and their oracles in tests/proof-estate.yaml state the skip.

Decide whether and when to remove the skip marker, for example after the release host's fseventsd is restarted and a diagnostic native run shows prompt delivery. Restoring the proofs is a test change that needs its own authorized correction and a fresh native qualification. No agent restores them automatically.
