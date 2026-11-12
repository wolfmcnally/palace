---
id: '28'
title: Native watcher post-readiness diagnostics
depends_on:
  - phase-26.md
  - phase-27.md
informs: []
review_lane: full
---

# Native watcher post-readiness diagnostics

## Commission

Authorized on 2026-11-11 through the authenticated factotum as the bounded diagnostic increment that the Phase 27 design review recommended as its own test-only phase. Phase 26's readiness barrier proved a delivering native stream before each deadline, yet its full gate still failed: after proven readiness, a target write produced no native callback and no overflow notification. The mechanism is unknown. This phase adds failure-only observations that let the next failure distinguish an in-process watcher lifecycle fault, a stalled stream, and target non-delivery, and say "indeterminate" when an observation is missing. It is diagnosis, not a fix, and makes no operating-system causal claim.

Every original assertion, deadline value and deadline start point stays as it is, and the release hold is unchanged. The phase does not change the qualification lane, deselect, skip, move, retry or host-qualify any proof, and does not accept a cross-host substitute. Both review and evidence lanes are full, with independent Opus plan review and code critique.

**Ownership of the carried diff.** The working tree carries Phase 26's uncommitted readiness barrier and stage probe in `tests/reindex/test_server.py` and its contract and oracle text in `tests/proof-estate.yaml`. Those changes belong to Phase 26, which stays parked and unaccepted with its failed gate receipt preserved. This phase builds on them as its base and owns only the increment it adds. Nothing here accepts, resumes or replaces Phase 26, and no earlier receipt is erased or reinterpreted. Whether a green candidate that necessarily carries Phase 26's changes is delivered is settled with the coordinator at close, not assumed.

## Goal

When a positive native watcher proof fails after readiness, its failure note states, from direct observation, whether the watcher's own callback raised, whether the emitter and observer threads were alive, whether the same stream still delivers a fresh canary after the deadline, and the resulting joint classification.

## Deliverables

- Callback outcome marks. The probe's wrapper around the emitter's event callback records the original callback's return or raised exception, with monotonic times, and captures records from the watchdog FSEvents logger, which reports exceptions the callback swallows. It records each canary and target event's complete flag word rather than three overflow bits, plus counts of root-changed and overflow flags for the watch root.
- Workload write marks. Each declared target write records its enter and return times, including both writes of the recovery proof, without changing the workload (no added fsync). The events-log writer stage records its fsync enter and return.
- Failure-time liveness. On a deadline failure the note records whether the emitter thread is alive, the observer's emitter set for the watch root, and whether the observer thread is alive.
- A failure-only post-deadline canary. After a deadline failure, a fresh canary is written and its native delivery is awaited within a bounded liveness window. The result and latency go into the note, and the original failure is re-raised unchanged.
- A joint classification line:

  | Emitter thread | Callback exception or logged error | Post-deadline canary | Classification |
  |---|---|---|---|
  | dead | any | any | in-process lifecycle |
  | alive | present | any | in-process lifecycle |
  | alive | none | not delivered | unresolved: stream stall, cause unknown |
  | alive | none | delivered, target never delivered | target non-delivery, not causal |
  | any observation missing or probe error | | | indeterminate |

- Proof-estate oracle text for the affected families updated to name the joint classification, at zero family and leaf growth.

## Acceptance

- Every original assertion, deadline value and deadline start point is unchanged from the Phase 26 base, shown by a diff of the assertion and deadline lines.
- On success no extra wait or write occurs; the post-deadline canary and liveness capture run only after a deadline assertion fails. No new collected test.
- Scratch-only fault controls, loaded through a pytest plugin outside the tree, each fail the test with the expected classification:
  - an exception raised inside the original callback after the canary: in-process lifecycle, with the logged error captured;
  - the emitter thread stopped after readiness: in-process lifecycle;
  - target events dropped while later events are delivered: target non-delivery;
  - every post-readiness event dropped: stream stall;
  - one observation suppressed: indeterminate.
  Each control passes when the plugin is absent.
- `./bin/test-governance validate` passes, with no family or leaf growth.
- In a coordinator-confirmed slot, one focused watcher-module run and then `./bin/check all`, one attempt each. A failure is reported with its joint classification and parks the phase truthfully; it is not retried.
- Manual: what the observed classification means for the qualification decisions recorded by Phase 27 belongs to the operator.

User Demo: N/A — test diagnostics with no product surface.

## Exclusions

The following are excluded:
- production source, watchdog upgrade or patch, and daemon overflow or rescan handling;
- any deadline, timeout, marker, skip, deselection, lane, continuous-integration or host-admission change;
- retry-until-green and cross-host substitution;
- accepting, resuming or replacing Phase 26, and any Phase 27 decision;
- the release, tags and publication;
- other planned follow-on work.

## Brief refs

None. This is a proof-diagnostics increment governed by the test-suite governance and verification policies.
