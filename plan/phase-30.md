---
id: '30'
title: Deterministic reindex pipeline proofs and a mandatory native watcher lane
depends_on:
  - phase-26.md
  - phase-27.md
  - phase-28.md
informs: []
review_lane: full
---

# Deterministic reindex pipeline proofs and a mandatory native watcher lane

## Commission

Authorized on 2026-11-11 by the operator, relayed through the authenticated factotum, as the implementation of the split-by-obligation option from Phase 27's reviewed design: "Yes split those especially due to the live macOS indeterminacy." The operator also ruled that the native watcher proofs stay mandatory for release, with their assertions and deadlines unchanged, no skip and no separate release waiver, and that the existing Phase 26 and Phase 28 diagnostic test changes are retained as the native lane's contents. The coordinator added that no cross-host substitution is inferred, that release requires a fresh successful native receipt bound to the candidate with every failed receipt retained, and that new and changed error paths in this work should name where in the stack a failure occurred, without logging secrets. It also corrected this phase's first capture: ordinary delivery, including this phase and the carried Phase 26 and 28 changes, is gated by one deterministic ordinary gate and never requires a native receipt. Native qualification belongs only to release. Phase 27's adopted watching-class delivery trigger is therefore superseded by the operator's wording, and its continuous-integration native step is a detector only.

Phase 27's plan review findings, all adopted in its decision addendum, are requirements here. Because the work amends governing prose, it runs as a prepared park followed by a fresh-capture continuation. One independent plan review and one independent code critique cover the whole phase across both runs.

**Carried working-tree changes.** Phase 26's and Phase 28's uncommitted test changes in `tests/reindex/test_server.py` and their contract and oracle text in `tests/proof-estate.yaml` become this phase's native-lane contents, unchanged in every assertion and deadline. Phase 29's wording and prompt changes, its proof admission, and the uncommitted proof-manager methodology correction in `lib/agentic_starter/test_governance.py` and `tests/test_test_governance.py` are not this phase's. This phase does not claim, accept, resume or reinterpret those parks or their receipts. Where it must touch `tests/proof-estate.yaml`, it owns only its own entries and its own share of the budget fields.

## Goal

Every platform's ordinary gate proves the reindex pipeline deterministically and never depends on native watcher delivery, and native macOS watcher behavior is proved by a separate, mandatory native lane whose candidate-bound receipt is executably required for release.

## Deliverables

- Deterministic end-to-end reindex pipeline proofs that run the real `serve()`, `build_observer`, handler, debouncer, writer and shutdown path, with only the native emitter replaced by a test observer built on watchdog's real observer and event queue. They run on macOS and Linux in the default gate.
- A registered native-lane marker on the existing native server proofs, which stay unchanged in every assertion, deadline and readiness barrier.
- A default test run that deselects the native lane, and a `./bin/check native` mode that runs exactly the native lane on macOS and records a durable receipt bound to the complete candidate it tested, its environment and its host, retaining every failed receipt.
- An executable release verification mode that refuses a missing, failed, stale, wrong-candidate or other-host full-gate or native receipt.
- A continuous-integration step that runs the native lane on macOS as a detector after push.
- Amendments to the build-gate and test-suite governance policies defining the full retained estate as the default lane plus the mandatory native lane, and the release checklist update.
- Proof-estate admission records for the new proofs under a named, exactly sized positive budget, and native-lane records for the retained native families.
- Diagnostic provenance in every new refusal and failure message: the gate, the candidate identity, the receipt or refusal reason, and the pipeline stage.

## Acceptance

- The deterministic pipeline proofs pass on macOS and Linux and each fails against a named mutant of the obligation it proves (red witness).
- The native server proofs' assertions, deadlines and readiness barriers are byte-identical to the carried Phase 26 and 28 candidate apart from the lane marker.
- `./bin/test` with no arguments, `./bin/check all`, `--vital` and `--changed-from` never run a native-lane proof; `./bin/check native` runs exactly the native lane, refuses off macOS, and records pass and failure receipts.
- The release verification mode refuses each of a missing, failed, stale, wrong-candidate and other-host receipt and a dirty tree, and passes with both current receipts for the same candidate.
- `./bin/test-governance validate` passes with the native families still in the inventory, the new admissions and the named budget, and no unrelated retirement.
- Both ordinary full gates pass before close. No native run is required to close or deliver this phase; native qualification is a release step, and a native failure there blocks only the release, is preserved, and is never retried automatically.
- Manual: whether the lane split reads as an honest account of coverage, and the release decision, belong to the operator.

User Demo: N/A — test-lane, gate and policy machinery with no new product surface.

## Exclusions

- Any change to a native proof's assertion, deadline, readiness barrier or diagnostic.
- Production reindex or watch source, except a refactor the stand-in observer strictly requires, named in the plan.
- Cross-host native qualification, host-load admission, retries, skips and longer deadlines.
- Production handling of overflow notifications.
- Phase 29's changes, the methodology correction, and the release itself.

## Brief refs

None. Authority is the Phase 27 reviewed design and its adopted findings, the build-gate, test-suite governance and verification policies, and the root invariants.
