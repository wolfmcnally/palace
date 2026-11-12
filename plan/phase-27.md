---
id: '27'
title: Native watcher qualification design
depends_on:
  - phase-26.md
informs: []
review_lane: full
---

# Native watcher qualification design

## Commission

Authorized on 2026-11-11 through the authenticated factotum as a separately scoped, design-only follow-up to the parked Phase 26. Phase 26 was a test-only readiness repair that forbade gate changes. Its implementation gate still failed after proven native readiness. A later attempt to carry an alternate-qualification proposal as a Phase 26 re-plan was refused by the plan-growth guard and parked. That guard's diagnosis stands: changing how native watcher proofs qualify a release is a separate scope, not a revision of the readiness repair.

This phase produces only a reviewed qualification-policy design for the operator's decision. It changes no test, gate, deadline, policy or source file, runs no test, and does not assume any option is accepted. Phase 26's runtime and test candidate stays parked and unaccepted, and this phase neither resumes nor replaces it. The release hold under the existing gate is unchanged. Independent review is advisory and runs on Opus.

## Goal

Give the operator an evidence-based, independently reviewed design for qualifying the native file-watcher proofs. It covers:
- which obligations stay unconditional in the default gate on every platform;
- which run in a required macOS native-qualification lane;
- how that lane is triggered and enforced before release;
- what each failure class means;
- why no coverage or safety is silently lost.

## Deliverables

- An evidence analysis of the native watcher failures on the development host and of the clean-host continuous-integration results. It distinguishes source identity from test identity, and the evidence's limits from its claims.
- A comparison of options:
  - keeping the native deadlines in the default gate;
  - a split by obligation, with deterministic default-lane proofs and a required native-qualification lane;
  - a lane move without replacement;
  - in-process lifecycle or isolation remedies, including a failure-only diagnostic increment.
- For the recommended option:
  - the exact file-level scope it would change;
  - the assertions that remain unconditional;
  - the triggering and fail-closed release enforcement of native coverage;
  - failure semantics;
  - the proof-estate and policy amendments it would require.
- Explicit operator decisions, recorded with one independent plan review and primary dispositions.

## Acceptance

- The design states every failure observation with its candidate identity and its mechanism limits. It makes no operating-system causal claim the evidence does not prove.
- Every reindex pipeline obligation currently proved only by the native proofs has a named unconditional proof route in the recommended option, or the option says explicitly that coverage would be lost.
- Native-lane enforcement is fail-closed for release and names its trigger, receipt and refusal condition.
- One independent advisory plan review is recorded with primary dispositions.
- No tracked file other than this phase file, its INDEX row and the activity log changes, and no test runs.
- Manual: the choice among the options, and every decision the design lists, belongs to the operator. Automated evidence does not settle it.

User Demo: N/A — design proposal with no product surface.

## Exclusions

The following are excluded:
- implementing any option, or any change to a test, gate, deadline, marker, continuous-integration workflow, release document, policy, proof-estate entry or production source;
- running any test or gate;
- resuming, accepting or replacing Phase 26;
- the release.

Production handling of overflow notifications remains a separate question.

## Brief refs

None. This is a qualification-policy design governed by the build-gate, test-suite governance and verification policies.
