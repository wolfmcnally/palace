---
slug: esoteric-hawk
title: Separate native event delivery from shutdown-flush evidence
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-04
source: kickoff
occurrences:
  - date: 2026-10-04
    ref: "Storage-error follow-up PARK: full gate shutdown test stopped after 0.3 seconds"
  - date: 2026-10-04
    ref: "Storage-error follow-up PARK: bounded repeat failed native delivery after readiness probe"
  - date: 2026-10-04
    ref: "Authorized exact-target and pending-shutdown correction: 43 focused tests; disabled-flush control rejected at missing persistence"
  - date: 2026-11-09
    ref: "Phase 24 full gate: missing-root watcher test received no alive.md event within two seconds"
  - date: 2026-11-11
    ref: "Phase 22 bearer correction handoff gate: shutdown witness received no burst.md submission within two seconds"
---

The existing shutdown test assumed that a 0.3-second wait meant its native file event had entered the debouncer. A full gate failed that assertion. A test-only prototype observed the actual accumulator and held its clock so shutdown_flush, rather than ordinary polling, had to emit the record. It passed focused runs, but broader native readiness prototypes failed repeatability and were discarded; no watcher production change or delivery-budget relaxation landed.

Registration, elapsed time and one delivered canary are proxies for future native event delivery. They cannot establish that the specific event under test has arrived. Require that exact event before claiming a pending-event shutdown witness, and retain native delivery failures separately. The observations do not identify whether scheduling, native stream behavior or another mechanism caused the intermittent failures. A dedicated bounded investigation remains necessary. These are candidate lessons, not graduated rules or a claimed watcher repair.

Follow-up as of 2026-10-04: the operator-approved bounded investigation completed sixteen direct application trials and four timestamped pytest module runs without reproducing the earlier failures. One successful native callback arrived 1.101 seconds after the completed write; the corresponding fsynced record arrived after 1.231 seconds. All three exact-write shutdown trials had already fsynced their burst record before shutdown, so their passes did not exercise pending-work drainage. No watcher source changes landed. The instrumentation may affect scheduling, and the original failures lack comparable stage timestamps; the intermittent mechanism remains unresolved. Preserve exact-target deadlines and record failure stages instead of treating another green retry as diagnosis.

Correction as of 2026-10-04: exact-target polling now rejects root replay under a single deadline; the two-second notes/alive deadline begins before the file write. The shutdown witness waits for the actual burst submission within two seconds and holds only the test clock, proving that ordinary polling cannot emit before shutdown. The real shutdown path must persist the pending record before returning. Its negative control disabled actual shutdown_flush and failed specifically at missing persistence, after native submission and clean worker termination. All 43 focused checks and the first full gate passed; the second handoff gate and delivery remained pending when this occurrence was written. No production watcher change or diagnosis of the earlier intermittent failures is claimed.

As of 2026-11-09, the Phase 24 implementation gate passed 267 tests and failed test_serve_skips_missing_on_disk_root: the exact alive.md event did not appear before its two-second deadline, with no records returned. The observer reported one live root after skipping the removed root. Reindex production code and this test were unchanged in the candidate; the new indexing proofs passed their focused run. This observation does not distinguish observer readiness, native delivery or later processing as the cause. The gate and complete diagnostics remain failed and preserved; no retry-until-green, timeout relaxation or watcher modification was performed. Scoped diagnosis is required before delivery can resume.

The authorized bounded investigation instrumented registration, native callback, handler dispatch, debounce and fsync without changing repository source. Eight target trials, the complete ten-test server module and one complete 268-test suite passed. Deliberately delaying startup and suppressing target processing at the handler both reproduced the original empty-record timeout; the stage timestamps distinguished them. These controls show why the original symptom cannot identify its cause. No natural failure was reproduced, and instrumentation can affect scheduling. Preserve the failed gate; these passing diagnostic runs establish neither a causal correction nor acceptance. A further bounded investigation needs failure-stage evidence under the original workload, not another uninstrumented green retry.

Qualification as of 2026-11-09: explicitly authorized failure-only stage instrumentation now accompanies the existing test. Fixed stage keys retain bounded first/last monotonic timestamps and counts for the synthetic target; the test's readiness sleep, exact assertion, two-second deadline and cleanup remain unchanged. Both deliberate controls still fail with discriminating stage notes. The ten-test module, one standalone 268-test qualification and the normal 268-test implementation gate passed; the separate handoff gate remained pending at this writing. This is a diagnostic improvement, not a causal correction. The original failure and its uncertainty remain preserved, and a new natural failure must stop for assessment with its stage evidence.

As of 2026-11-11, the bare handoff gate for the Phase 22 Bedrock bearer-authentication correction passed 267 tests and failed test_serve_sigterm_drains_inflight_debouncer: after the observer was reported ready, the exact burst.md submission did not reach the debouncer within its two-second deadline. The candidate-bound implementation gate had passed all 268 tests on the same product candidate minutes earlier, and no reindex source or test changed. This test carries no failure-only stage instrumentation, so the stage at which delivery stalled is unknown. The gate stays failed and preserved; no retry-until-green, deadline change or watcher modification was made, and delivery parked for the operator.
