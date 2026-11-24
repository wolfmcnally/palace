---
title: Reconcile the root's implementation-gate order with the kickoff validator
scope: methodology
proposed_surface: policy
source: kickoff
slug: annoying-mackerel
status: candidate
filed: '2026-11-24'
occurrences:
- date: '2026-11-24'
  ref: 'us-bedrock-profile plan review PLAN-F002 (declined): reviewer cited AGENTS.md
    Two full gates against the plan''s validator-required gate order'
---

As of 2026-11-24, AGENTS.md's Two full gates invariant says the orchestrator runs the implementation-candidate sequence ending in `./bin/check all`, while the kickoff acceptance resource and its pinned validator (`kickoff-evidence validate --required-final-command "./bin/test --changed-from '@{upstream}'"`) require the changed-path selection as the final implementation-gate command. An independent plan reviewer reading the root rule flagged the prescribed order as a defect; the primary declined it because the validator refuses the other order.

One owner should state the order, and the other surface should defer to it, so reviewers and orchestrators read the same rule. This is an unratified candidate for the operator.