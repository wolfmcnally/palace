# Policies — meta

`policies/` holds the binding rules that govern how palace is built and operated. Every agent working in this repo reads policies *on demand* — when the action it's about to take is governed by one. `CLAUDE.md` is the always-loaded identity card; `policies/` is the spec library beneath it.

## What goes in `policies/`

- **Binding rules** that constrain future implementation, organization, or operation.
- **Format specifications** that downstream code must conform to (file layouts, schemas, naming).
- **Process contracts** that span multiple actions or agents.
- **Tunable parameters** with documented rationale, when the right value is non-obvious or has decay-prone justification.

A policy file is the right home when:

1. The rule is too long or too detail-heavy to live in `CLAUDE.md`, and
2. Future work will cite it as authoritative, and
3. It should outlive any single decision, brief, or cycle.

## What does NOT go in `policies/`

- **Research, surveys, methodology investigations.** Those are briefs (`briefs/`). A brief *informs* a policy; the policy is the rule extracted from it.
- **Logs of what happened.** Those are commits and (eventually) `LOG.md`.
- **Active execution queues.** Live queues are directories at the repo root (`user-actions/` per [`user-actions.md`](user-actions.md)), not policy bodies. A policy here may *govern* a queue's format and lifecycle; the queue itself does not live under `policies/`.
- **Identity / framing material** that every agent needs every turn. That stays in `CLAUDE.md`.

## How a policy evolves

- **Add** a policy when a rule starts being cited or implicitly assumed in more than one place. Don't pre-write speculative policies.
- **Revise** in place when the rule changes; bump no version metadata, just edit. Git is the audit trail.
- **Supersede** by replacing the file's contents and noting the prior shape inline if the prior shape will be referenced. Don't keep dead policies around just for history.
- **Cite the brief** that motivated the policy when the brief's argument is the load-bearing justification. The policy is the rule; the brief is why.

## Authority and precedence

When two rules conflict:

1. The operator's explicit instructions and applicable harness instructions override everything in palace.
2. Palace's `CLAUDE.md` overrides individual policy files.
3. Policy files override briefs (briefs inform, policies bind).
4. `plan/` files override briefs (the plan is the refinement — it knows what the brief did not; update the brief to record the refinement).
5. A more-specific policy overrides a more-general one.

If an apparent conflict can't be resolved by precedence, surface it. Don't paper it over.

When the operator explicitly overrides a policy in-session for a clearly-scoped reason, the override is one-shot — the policy survives unless he asks for it to be amended.

The full contract *between* `briefs/`, `policies/`, and `plan/` — who cites whom, who wins on conflict, and the common drift modes (policy disguised as brief, plan disguised as policy, brief disguised as plan) — is [`briefs-and-policies.md`](briefs-and-policies.md).

## How agents use this directory

- The planner reads the policies that touch the phase's surfaces before drafting a plan.
- The plan reviewer and code critic treat every policy as a blocking criterion: a plan or diff that violates a policy is `REVISE`.
- The coder honors every policy while writing code.

## Format

Policies are Markdown. No frontmatter required (unlike briefs). Lead with a one-paragraph statement of what the policy governs. Use H2/H3 for structure. Keep them dense; resist the urge to explain things twice.

## Current policies

See the other files in this directory.
