---
id: '26'
title: Native watcher readiness witnesses
depends_on:
  - phase-25.md
informs: []
review_lane: full
---

# Native watcher readiness witnesses

## Commission

Authorized on 2026-11-11 through the authenticated factotum as the existing bounded gate repair for the native file-watcher proofs. These proofs failed the 0.5.0 release's full gate twice on an unchanged metadata-only candidate. The scope is test-only: prove an actually delivering native stream before each test clock starts, and attach failure-only diagnostics that distinguish startup readiness, delayed delivery after readiness, and file-system overflow notifications. Every original event, debouncer, recovery and shutdown obligation, every deadline and its start point, and every proof family stay as they are. A readiness barrier is not a promised fix, and a late arrival never turns a failure into a pass. Both review and evidence lanes are full, with independent Opus plan review and code critique; Opus only. Ordinary guarded commit and fast-forward push are authorized. Release publication remains a separate follow-up. Production handling of overflow notifications is excluded and documented as an observed source gap, not a demonstrated cause.

## Goal

Make each positive native watcher proof start its deadline only after the same native stream has delivered a canary event. On failure, report which stage the target reached, whether it arrived late, and whether an overflow notification replaced it.

## Deliverables

- A shared test readiness barrier. It writes a canary file that the daemon's handler already ignores, and waits with a readiness timeout separate from each test's deadline until the native callback delivers that exact path. It replaces the fixed startup sleep and the registration-return proxy in every positive native proof in tests/reindex/test_server.py.
- Shared failure-only stage diagnostics for every positive native proof. They record:
  - native callbacks for the watch root: count, event-identifier range, and overflow flags (must-scan-subdirectories, user-dropped, kernel-dropped);
  - readiness time;
  - target write;
  - the target's handler, debouncer submit and emit, and fsync stages.
- After a deadline failure, a bounded diagnostic-only observation that classifies the target as late, superseded by overflow, or never delivered. The failure is preserved.
- Proof-estate contract, oracle and mutation text updated for the affected families, at zero family and leaf growth.

## Acceptance

- Every original assertion and deadline value is unchanged: exact-target polling, the two-second and five-second deadlines measured from before the target write, debouncer entry, recovery after one handler exception, shutdown flush and persistence, the dotfile and gitignore negatives, the missing-root skip and daemon return code. Each deadline now starts only after proven readiness.
- The canary never reaches the debouncer, events log or any record assertion. The negative-path proofs keep their meaning.
- The canary must not consume test-injected behavior. The handler's ignore filter runs after `on_created` and `on_modified` (palace/reindex/observer.py `_handle_simple` → `_dispatch`). So the recovery proof's single injected handler exception is armed only after readiness, and must still be raised by the first workload write.
- Fault controls fail at the intended stage with a classified diagnostic:
  - canary suppressed: the barrier times out as readiness failure, not as a target-deadline failure;
  - target delivery suppressed after readiness: a deadline failure classified as never delivered;
  - handler suppressed: native arrival without debouncer entry;
  - synthetic overflow event injected: classified as overflow.
- A late arrival inside the diagnostic window still fails the test.
- Focused watcher module repeat runs and both complete repository gates pass against their fixed candidates, with independent plan and code advice and primary dispositions retained.
- Manual: whether the two-second post-readiness deadline should remain a host-load-sensitive gate assertion is an operator judgment. Automated evidence does not settle it.

User Demo: N/A — test-proof correction with no product surface.

## Exclusions

No production source change, watchdog upgrade or patch, rescan or overflow handling in the daemon, deadline or timeout change, test skip, retry-until-green, host-load admission, new collected test, release, tag or publication. Linux behavior is unchanged; these proofs remain macOS-only.

## Brief refs

None. This is a proof-correctness repair governed by the test-suite governance and verification policies.
