---
title: "Harness Self-Improvement: The Two-Tier Flywheel"
date: 2026-08-10
status: implemented
scope: How palace captures process lessons at phase scale, prunes its rule surfaces on a cadence, and exports the generalizable subset upstream to the methodology it is built on.
---

# Harness Self-Improvement: The Two-Tier Flywheel

Palace is two things at once: a memory substrate under construction, and a harness — skills, agents, policies, briefs, and deterministic scripts wrapped around whichever coding-agent CLI hosts a session. A harness that only accumulates rules by hand improves at the speed of its operator's memory. Current best practice is to make improvement *structural*: every unit of work ends by asking what was learned, learnings accumulate as addressable entries rather than prose rewrites, recurring ones graduate into rules under human ratification, and the rule surfaces themselves are pruned on a cadence so the compounding asset never turns into a compounding liability.

This brief records the design as palace runs it: what the loop is, where each stage lives, what was deliberately declined, and what is deferred. The machinery was absorbed from the agentic-coding starter template on 2026-08-10 (`/learn`); this brief is palace's adaptation of that design, not a copy of the template's own account of it.

## 1. The two tiers

The improvement flywheel runs at two scales:

- **Phase scale (inner tier, runs in palace).** `kickoff` closes each phase with a harvest step: the four roles' Process Observations, the coder's failure analyses, review verdicts, dispositions, and wall-clock observations are distilled into candidate lessons in the [`lessons/`](../lessons/) ledger. Recurring lessons surface as graduation proposals the operator ratifies (or doesn't). The `sweep` skill periodically audits the accumulated rules, skills, and briefs and proposes retirements.
- **Repo scale (outer tier, runs between repos).** Palace is a **spoke**, not the hub. The starter template is the hub: it ships the methodology, and its own `learn` pass harvests palace's `scope: methodology` lessons back upstream, where — once ratified there — they improve every future project stamped from the template. Palace's `learn` and `teach` skills run the same channel in both directions for any other repo the operator points them at.

The seam between the tiers is the lesson's `scope` field. A `local` lesson stays in palace; most palace lessons are local, because most of what palace learns is about stores, daemons, bitemporal facts, and retrieval. A `methodology` lesson is a standing export: pre-digested, provenance-carrying input the hub reads as a first-class source instead of rediscovering the pattern from raw files.

Palace's own dependency runs the other way too. Palace is downstream of the template, so a methodology defect palace hits *first* — the timeout recalibration contract, the evidence schema, the review lanes — is worth exporting precisely because palace exercises the machinery at a scale the template itself never does.

## 2. The loop, stage by stage

1. **Capture.** Roles emit Process Observations (friction or ambiguity in briefs, policies, plans, tooling, or the methodology itself) as structured output fields; on revision rounds the coder states *why* the previous attempt failed, and that analysis travels in the revision packet. `kickoff`'s harvest step files or recurs lessons in `lessons/` — one file per lesson, scope-classified `local` or `methodology`, with a new occurrence appended rather than a duplicate filed. "No lessons this phase" is a permitted, recorded answer; skipping the question is not.
2. **Distill.** `bin/lessons` mechanically validates the ledger and tallies occurrences; `bin/lessons candidates` lists graduation-ready entries (three or more occurrences). Three is the stabilization threshold: a lesson codified on first sight tends to be wrong in ways its variations would have revealed.
3. **Codify.** Graduation is human-ratified, one lesson at a time, onto a named surface: a policy, a brief, a skill, an agent definition, a `bin/` script, a test, or a `CLAUDE.md` invariant. Agents propose; the user edits or approves the edit; the lesson archives with `graduated_to:` pointing at the rule it became. Every rule stays traceable to the incidents that earned it.
4. **Prune.** `sweep` runs the maintenance half: stale or contradictory rules, skills past their review cadence, briefs due for `historical` status, aging ledger candidates, catalog drift, internal-link integrity (`bin/check-catalogs`), and the proof estate's executable reassessment and shrinkage obligation (`bin/test-governance reassess`). Palace is not a template, so `sweep`'s hub-only methodology-corpus audit self-disables — it keys on `.claude/skills/stamp/`, which palace does not have. Two further passes read the review loops rather than the rules: `sweep-planning` and `sweep-coding` harvest every genuine plan-review or code-review verdict and every coder failure analysis from the machine's harness traces (`bin/review-verdicts`), categorize why work was sent back over the window, attribute each category to a correctable planner or coder defect, a reviewer or critic habit, or a structural gap, and — because palace is a derived project — file the corrections as `scope: methodology` lessons for `learn` rather than editing the personas here. Both enter plan mode first and present analysis and plan together for ratification.
5. **Propagate.** The generalizable subset leaves through `scope: methodology`. Applying a pattern is an empirical test of that pattern's contract, so a defect palace discovers while *using* the methodology is itself methodology evidence, and `learn`'s return path exists to keep it from evaporating.

Rule One surrounds this loop rather than replacing it. A correction, failed
command, surprise, or discarded approach first triggers diagnosis of the
mechanism and the nearest durable prevention surface. Only reusable findings
enter the lessons ledger; one-off repairs remain local evidence, and mechanical
defects prefer an executable guard over another prose reminder. The operative
procedure is [`.claude/skills/rule-one/SKILL.md`](../.claude/skills/rule-one/SKILL.md),
with rationale in [`rule-one-diagnostic-learning.md`](rule-one-diagnostic-learning.md).

## 3. Why itemized capture, not instruction-file rewrites

The naive loop — "agent, update `CLAUDE.md` with what you learned" — degrades under iteration: each rewrite shortens and blands the document (brevity bias) until accumulated knowledge collapses into generic filler (context collapse). Palace's `CLAUDE.md` is a load-bearing always-loaded surface; it is exactly the document that must not be rewritten from session memory. The remedy is structural: lessons are discrete, addressable files with provenance and occurrence counters; the author of a lesson is never the authority that ratifies it; and rule documents are only ever edited by the human, deliberately, one graduation at a time. The ledger grows; the rules are curated.

This is the same separation palace already applies to its own subject matter — capture is the hot path, extraction is the cold path, and promotion is gated. The lessons ledger is that architecture turned on the harness itself: Process Observations are the raw events, the harvest is the consolidator, and graduation is promotion under a human gate.

## 4. Grounding

The design instantiates named patterns from the Encyclopedia of Agentic Coding Patterns, checked against the 2026 self-improving-harness literature:

- **Compound Engineering** — codification as a closing condition of every unit of work. The harvest step is that closing condition made mandatory.
- **Feedback Flywheel** — capture → distill → codify with a recurrence threshold before rules land.
- **Reflexion** — the coder's failure analysis on revision rounds, stored in Change Evidence and fed forward in the revision packet.
- **Garbage Collection / Skill Fitness** — `sweep` and the skills' `last-reviewed` cadence.
- **Incident-to-Eval Synthesis** — dispositions and post-mortems route into lessons, and mechanizable fixes land with regression tests in the same change.
- **Agentic Context Engineering** — itemized, tagged, counter-scored entries with a separated curator role (here: the operator), replacing monolithic self-rewrites.
- **Self-Harness (arXiv 2606.09498) and successors** — weakness mining from execution traces (`recommend-timeouts` surfacing at phase close is the first instance), bounded minimal proposals, regression-gated acceptance, and strict separation between the thing evolving and the evaluator judging it (`./bin/check all` never bends to a lesson).

## 5. Deliberately declined

- **Autonomous self-modification.** The fully closed loop — agent mines weaknesses, edits its own rules, validates, merges — contradicts the separation between agent proposals and user-ratified rule authorship, and imports the literature's own top risks (reward hacking, evaluator contamination). The user is the curator; that is a design choice, not a maturity gap.
- **First-pass-acceptance-rate tracking.** The flywheel's canonical metric needs a denominator that only becomes meaningful over many phases, and gaming pressure makes it a trend indicator at best. Palace already carries exact per-role execution telemetry; revisit once that ledger has enough closed phases for a rate to mean something.
- **Skill lift measurement.** Measuring each skill's marginal effect on task pass-rate requires an eval harness aimed at the *harness*, not at retrieval. The product evaluation targets recall quality, which is a different question. `last-reviewed` cadence plus `sweep` retirement proposals is the proportionate version at this scale.
- **Hooks as a codification surface.** Palace's tracked hooks stay opt-in (`bin/install-hooks`); lessons that need deterministic enforcement route to `bin/` scripts and gates instead — which is where `bin/check-catalogs` came from.

## 6. Relationship to palace's own memory substrate

Palace is building a system that ingests agent sessions and promotes durable facts out of them. The lessons ledger looks adjacent, and the boundary is worth stating so neither absorbs the other:

- **Palace's fact store** holds operator-authorized domain facts extracted by consolidation, bitemporal, provenance-hashed, machine-scored.
- **The lessons ledger** holds process learnings about *how work in this repo is done*, written deliberately at phase close, ratified by a human, and destined for a rule surface rather than a query.

They are not merged, and the ledger is not a `palace search` corpus. Per [`CLAUDE.md`](../CLAUDE.md)'s "Rules, not memory" invariant, anything that must bind future sessions belongs in committed repo files — not in an index that a retrieval change could silently stop surfacing. If palace later wants the ledger searchable, that is an *additional* consumer of the same committed files, never a relocation of them.

## 7. Acceptance criteria for this design

- A phase close cannot complete without answering the lessons question: the END block's `Lessons:` field is mandatory.
- `./bin/lessons validate` and `./bin/check-catalogs` pass as part of `./bin/check all`, covering ledger schema, catalog membership, tracked internal links, and phase-lifecycle state.
- A revision round cannot capture Change Evidence without a nonempty `failure_analysis`, and the revision packet carries it to the critic.
- A `scope: methodology` lesson filed in palace is visible to an upstream `learn` pass without bespoke exploration.
- No agent-authored change to `policies/`, `briefs/`, `CLAUDE.md`, a skill, or an agent definition cites a lesson as its authority without a recorded human ratification.

## Sources

- Encyclopedia of Agentic Coding Patterns (aipatternbook.com): `compound-engineering`, `feedback-flywheel`, `garbage-collection`, `skill-fitness`, `incident-to-eval-synthesis`, `agentic-context-engineering`, `reflexion`, `harness-engineering`.
- *Self-Harness: Harnesses That Improve Themselves*, arXiv:2606.09498 (2026) — the weakness-mining → bounded-proposal → regression-validation loop.
- Lilian Weng, "Harness Engineering for Self-Improvement" (July 2026) — editable-surface taxonomy, evaluator isolation, "humans move up the stack."
- Fowler/Boeckeler, "Harness engineering for coding agent users" (martinfowler.com) — the three-loop model; this brief's machinery is the outer loop given repo surfaces.
- [`briefs/methodology.md`](methodology.md) — the eleven-step pipeline this flywheel closes over.
