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
---

The existing shutdown test assumed that a 0.3-second wait meant its native file event had entered the debouncer. A full gate failed that assertion. A test-only prototype observed the actual accumulator and held its clock so shutdown_flush, rather than ordinary polling, had to emit the record. It passed focused runs, but broader native readiness prototypes failed repeatability and were discarded; no watcher production change or delivery-budget relaxation landed.

Registration, elapsed time and one delivered canary are proxies for future native event delivery. They cannot establish that the specific event under test has arrived. Require that exact event before claiming a pending-event shutdown witness, and retain native delivery failures separately. The observations do not identify whether scheduling, native stream behavior or another mechanism caused the intermittent failures. A dedicated bounded investigation remains necessary. These are candidate lessons, not graduated rules or a claimed watcher repair.

Follow-up as of 2026-10-04: the operator-approved bounded investigation completed sixteen direct application trials and four timestamped pytest module runs without reproducing the earlier failures. One successful native callback arrived 1.101 seconds after the completed write; the corresponding fsynced record arrived after 1.231 seconds. All three exact-write shutdown trials had already fsynced their burst record before shutdown, so their passes did not exercise pending-work drainage. No watcher source changes landed. The instrumentation may affect scheduling, and the original failures lack comparable stage timestamps; the intermittent mechanism remains unresolved. Preserve exact-target deadlines and record failure stages instead of treating another green retry as diagnosis.

Correction as of 2026-10-04: exact-target polling now rejects root replay under a single deadline; the two-second notes/alive deadline begins before the file write. The shutdown witness waits for the actual burst submission within two seconds and holds only the test clock, proving that ordinary polling cannot emit before shutdown. The real shutdown path must persist the pending record before returning. Its negative control disabled actual shutdown_flush and failed specifically at missing persistence, after native submission and clean worker termination. All 43 focused checks and the first full gate passed; the second handoff gate and delivery remained pending when this occurrence was written. No production watcher change or diagnosis of the earlier intermittent failures is claimed.
