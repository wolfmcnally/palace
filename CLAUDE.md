# CLAUDE.md

This file provides guidance to coding agents (Claude Code, Codex CLI, and others that read top-level instruction files) when working in this repository.

## Hard rules — read these before any action

These rules govern every action in this repo. They are placed first so an agent reads them before doing anything irreversible. The full policy text for each is in `policies/`; consult that before bending a rule.

1. **Deliver gate-proved work; the user owns judgment and the destructive Git surface.** Once a phase closes with every gate green, the orchestrator uses `./bin/deliver` to commit and fast-forward-push it — binding the live handoff candidate, staging only its explicit paths, and verifying the staged and committed path sets, with no agent credit and never `--no-verify`. Delegated roles never commit or push. Everything destructive stays the user's: tags, force-pushes, branch deletion, reset, rebase, checkout-based restoration, clean, remote creation or selection, and history repair or rewrite. A candidate mismatch, unexpected path, hook refusal, missing or ambiguous upstream, rejected push, divergence, or residual dirt **parks delivery**; never work around it. **Delivery is not acceptance.** Manual, perceptual, product, readability, custody, policy, and brief-authorship judgments — including the phase's `User Demo:` protocol — stay open for the user after delivery. Failed executable gates and open `DECIDE` items block close. Full policy: [`policies/human-in-the-loop.md`](policies/human-in-the-loop.md).

2. **Greenfield until released: no backward-compatibility code.** Do not write legacy aliases, `@deprecated` markers, schema migrations to read older formats, transitional code paths, version-conditional branches, or "compat" shims of any kind. When an earlier shape turns out wrong, replace it directly and update every call site, fixture, test, sample data file, brief, plan, and doc in the same phase. This rule ends only when palace ships a stable external release and explicitly amends the policy. Full policy: [`policies/greenfield-until-released.md`](policies/greenfield-until-released.md).

If the user explicitly restricts or waives one of these rules for a named scope, record the instruction verbatim in the phase's END block. Restrictions and waivers are one-shot; the next phase reverts to the default. A delivery restriction narrows delivery only — it never relaxes a gate or closes a parked criterion.

<!-- PROJECT_CONTEXT_START -->

# Project Context

## Palace

Palace is a personal-memory substrate for agents and applications. Preserve provenance, local ownership and interchangeable consumer surfaces. Runtime source lives at repository-root `palace/`; metadata, lockfile and tests also live at root. This is an intentional flat layout, not an isolated `project/` deliverable.

## Product invariants

- **Files are the API.** Markdown with YAML frontmatter owns semantic facts; JSON/JSONL owns structured streams. Databases and search/graph indexes are derived and rebuildable. Never store the sole copy of information in an index.
- **Local-first.** Default embeddings, reranking and graph operations run on-device. Third-party embedding requires an explicitly opted-in already-published corpus. Private HTTPS inference requires an explicit operator-controlled boundary attestation and the native adapter; neither path permits the personal store or personal-vault overlap. Apply the binding [local-first policy](policies/local-first.md) before network access; never substitute providers.
- **Bitemporal facts and provenance.** Preserve event time and ingestion time. Facts are invalidated or superseded, never deleted; provenance lists source-event SHA-256 identifiers. Standard Markdown, JSON and hashing tools must recover every canonical field.
- **Capture is hot; extraction is cold.** Capture hooks and event handlers append raw events quickly; scoring, extraction, contradiction detection and promotion belong to consolidation. `events/`, `sessions/` and `raw/<source>/` remain append-only; retractions are appended events.
- **Session snapshots stay frozen.** Context injected at session start is read once; mid-session writes become visible next session. Dynamic recall uses `palace search`.
- **One substrate, many consumers.** Preserve parametric independent machine-store and human-vault roots. Consumer applications may expose wikilinks, hierarchical tags and other conveniences but must not become requirements of the canonical layer.
- **Daemon declarations are infrastructure.** Every daemon has a tracked launchd plist. Do not introduce untracked daemons or console-only lifecycle setup. Linux synchronous support does not imply porting macOS daemons.
- **Eval-driven retrieval.** Before tuning retrieval parameters or swapping a reranker, the personal evaluation set must exist and score the change ([sota memory brief](briefs/sota-memory-and-recall.md), §E.2; [retrieval policy](policies/retrieval.md)). A synthetic benchmark does not satisfy this prerequisite. The shipped default-on reranker exception allowed default-on reranking before the personal evaluation; its first run must confirm or reverse that decision. Preserve the exception and its declared validity gap.
- **No consumer disclosures.** All upstream artifacts describe capability classes, never consuming projects or their private identifiers. This includes existing files, examples, fixtures and logs. Remove discovered references from the current tree; public release also requires a separate disposition of historical copies.

## Product briefs

- [Memory System Improvements Plan](briefs/Memory-System-Improvements-Plan.md)
- [consolidator extraction source pluggability](briefs/consolidator-extraction-source-pluggability.md)
- [contextual enrichment](briefs/contextual-enrichment.md)
- [deployed consumer readiness](briefs/deployed-consumer-readiness.md)
- [embedding provider and index identity](briefs/embedding-provider-and-index-identity.md)
- [eval fixture corpus](briefs/eval-fixture-corpus.md)
- [eval set candidates](briefs/eval-set-candidates.md)
- [project index generator](briefs/project-index-generator.md)
- [remote embedding throughput](briefs/remote-embedding-throughput.md)
- [reranker defaults and providers](briefs/reranker-defaults-and-providers.md)
- [sota memory and recall](briefs/sota-memory-and-recall.md)
- [store parametric external consumers](briefs/store-parametric-external-consumers.md)
- [structured external indexing](briefs/structured-external-indexing.md)
- [tooling](briefs/tooling.md)

## Product policies

- [consolidation](policies/consolidation.md)
- [fact schema](policies/fact-schema.md)
- [local first](policies/local-first.md)
- [policies](policies/policies.md)
- [retrieval](policies/retrieval.md)
- [storage layout](policies/storage-layout.md)
- [surface vs substrate](policies/surface-vs-substrate.md)

## Runtime, operations and models

Python 3.12 with uv, Ruff, mypy and pytest. `./bin/setup`, `./bin/test [args...]`, `./bin/check all` and `./bin/python` own the locked toolchain. The full gate includes lint, formatting, type checking, tests, CLI smoke and policies. Preserve authoritative override refusal and hermetic automated tests.

The editable system-wide uv tool has a separate dependency environment from the repository environment. Source changes need no reinstall; dependency changes require `uv tool install --editable . --reinstall` and an explicit reinstall note in the phase END block. Supervised daemons use their repository environment.

`kickoff.yaml`: model/effort pins, timeouts, research budgets. `roles` edits pins or expands presets. Default: eligible primary plans/codes inline; cross-provider advisory review is preferred, with a fresh same-provider instance when unavailable or disallowed. Constrained primaries use delegated approval gates. Live preflight precedes mutation.

`palace search` is this library's hybrid retrieval API; a consuming agent's similarly named memory tool is a separate call surface. Preserve Markdown semantics on ingest. Integrations are deliberate and opt-in, never inferred from a sibling directory existing.

`docs/` retains project operations and evaluation artifacts. Pinned external references have a separate catalog under `docs/reference/`; operational documentation is not vendor text. `kickoff.yaml` owns role/model/effort choices. Primary routing uses Astra/high in Codex and Opus/high in Claude Code, with cross-provider advisory review, same-provider fallback and live preflight. The selected Claude-led configuration uses Astra advisory review: the Claude plan reviewer and code critic are Astra/high in primary and delegated routing. Existing delegated pins remain available. Other configuration stays target-owned.

Delivery uses the required `./bin/deliver` guard under the hard rule above. Its complete-tree staging identity and separate `--delivery` projection preserve deletion-aware custody through commit. Never substitute handwritten Git delivery for this guard.

<!-- PROJECT_CONTEXT_END -->

<!-- METHODOLOGY_CONTRACT_START -->

# Methodology Contract

## Methodology briefs

- [Eleven steps, doctrine and glossary](briefs/methodology.md)
- [Diagnosis and durable learning](briefs/rule-one-diagnostic-learning.md)
- [Portable bootstrap procedure](briefs/agentic-bootstrap.md)
- [Cross-CLI invocation contracts](briefs/cross-agent-invocation.md)
- [Candidate-bound incremental assurance](briefs/incremental-orchestration.md)
- [Command authority and custody](briefs/deterministic-orchestration-control-plane.md)
- [Draft deterministic workflow design](briefs/deterministic-orchestration.md)
- [Lessons and maintenance flywheel](briefs/harness-self-improvement.md)
- [Dated loading and continuity guidance](briefs/session-context-compaction.md)
- [Proof-estate design](briefs/test-suite-value-governance.md)
- [Minimal methodology scaffold](briefs/mini-method.md)

## Policies catalog

Every applicable policy binds.

- [Policy catalog](policies/README.md)
- [Brief lifecycle](policies/briefs.md)
- [Authority direction](policies/briefs-and-policies.md)
- [Reference pins](policies/docs.md)
- [Mirrors and loading](policies/cross-harness-parity.md)
- [Roles and convergence](policies/four-canonical-agents.md)
- [Role routing](policies/role-models.md)
- [Role deadlines](policies/role-timeouts.md)
- [Research authority](policies/research-authority.md)
- [Evidence and close](policies/orchestration-evidence.md)
- [Command custody](policies/orchestration-control-plane.md)
- [Timing and reports](policies/execution-telemetry.md)
- [Scripts versus judgment](policies/mechanistic-vs-intelligence.md)
- [Staging](policies/commit-staging.md)
- [Build gates](policies/build-gates.md)
- [Test governance](policies/test-suite-governance.md)
- [Park/resume](policies/fail-closed-resume.md)
- [Review lanes](policies/review-lanes.md)
- [Phase state](policies/phase-status.md)
- [Ripple](policies/phase-ripple.md)
- [Acceptance](policies/acceptance-empirical.md)
- [User demos](policies/user-demo-protocols.md)
- [Treatise](policies/treatise.md)
- [Verification](policies/verification-discipline.md)
- [Log](policies/log-discipline.md)
- [User actions](policies/user-actions.md)
- [Lessons](policies/lessons.md)
- [Human judgment](policies/human-in-the-loop.md)
- [Paths](policies/repo-relative-paths.md)
- [Isolation](policies/project-isolation.md)
- [Simplicity](policies/simplicity-and-consolidation.md)
- [Greenfield](policies/greenfield-until-released.md)

## Universal repo layout

[docs catalog](docs/README.md): operations and evaluation; [reference catalog](docs/reference/README.md): third-party pins; [bin catalog](bin/README.md): executables. `lib/agentic_starter/`: shared machinery; `tests/`: independent proofs. `.githooks/`: opt-in via `bin/install-hooks`, witnessed by `bin/check-hooks-installed`. `reports/execution/`: sanitized reports; `reports/test-governance/`: recipient-local proofs. `LOG.md`: history; `user-actions/` and `lessons/`: per-file queues with archive directories.

### Universal skills

- [kickoff](.claude/skills/kickoff/SKILL.md)
- [methodology](.claude/skills/methodology/SKILL.md)
- [rule-one](.claude/skills/rule-one/SKILL.md)
- [learn](.claude/skills/learn/SKILL.md)
- [teach](.claude/skills/teach/SKILL.md)
- [roles](.claude/skills/roles/SKILL.md)
- [sweep](.claude/skills/sweep/SKILL.md)
- [sweep-planning](.claude/skills/sweep-planning/SKILL.md)
- [sweep-coding](.claude/skills/sweep-coding/SKILL.md)
- [demo](.claude/skills/demo/SKILL.md)
- [treatise](.claude/skills/treatise/SKILL.md)
- [plain](.claude/skills/plain/SKILL.md)
- [ask](.claude/skills/ask/SKILL.md)

### Canonical roles and mirrors

- [phase-planner](.claude/agents/phase-planner.md)
- [plan-reviewer](.claude/agents/plan-reviewer.md)
- [phase-coder](.claude/agents/phase-coder.md)
- [code-critic](.claude/agents/code-critic.md)

`.claude/` is canonical. `.agents/skills/<name>` → `../../.claude/skills/<name>` exposes all resources. `.codex/agents/<role>.toml` is a thin canonical pointer with matching description. Edit canonical sources. Product phases use `kickoff`; methodology follows the routing rule below.

## Phase work and the `kickoff` skill

Invoke `/kickoff` (Claude Code) or `$kickoff` (Codex). Read the [kickoff](.claude/skills/kickoff/SKILL.md) and each linked resource before its branch, including follow-ups/recovery; links do not prove reads. Limits: root 20480 UTF-8 bytes; entry 10240. Preserve obligations and catalogs; move explanation to its owner.

### Status markers

Only [plan/INDEX.md](plan/INDEX.md) holds status: ⏳ not started, ⬅️ next, 🚧 in progress, ✅ completed. One marker per row; idle incomplete work has one arrow, active/complete work may have none; never multiple arrows. Explicitly select active work to resume. `kickoff` owns transitions; no per-phase `status`.

### Reading protocol for phase work

1. Read `plan/INDEX.md` for dependencies and cross-cutting concerns.
2. Read the parent phase if targeting a child, then the target phase.
3. Read every Brief ref and every pinned document on which it depends.
4. Read every `depends_on` file and the immediately preceding completed phase.
5. Read applicable policies, root invariants and the stage resource. Read only required phase files.

### Architectural invariants (load-bearing — do not violate)

Rationale: [methodology](briefs/methodology.md#operating-invariants-and-vocabulary).

- **Rules, not memory; Rule One.** Keep durable cross-harness knowledge in its owning repo authority. Diagnose failures, corrections, surprises and discarded work; separate containment, correction and prevention. File unsettled learning in `lessons/` and harvest every END/PARK, including `none`; only the human graduates rules.
- **Monotonic progress.** Hold authorized scope fixed; defer tangents once. Stop unsupported expansion. Dispatch authorized unblocked work or state the hold; inspect any command refusal before continuing.
- **Evidence over proxies.** Name the authoritative property, proxy, innocent triggers and sign-inversion risk. Read cited identifiers at their definitions; grep is a lead. Recheck affected callers, fixtures, tests and independent inventories. Refusals are inspected before subsequent commands; empty inspection is not successful verification.
- **Concrete uses; one home.** No speculative abstraction without a second present use. Consolidate at three copies; prefer fewer concepts. Use scripts for deterministic work and intelligence for contextual judgment.
- **Authority direction.** Policies prevail; plans refine and outrank briefs. Fix ambiguity at its owner; briefs never cite policies or plans.
- **Coherent outcomes.** Multiple surfaces and absent children do not require splitting. Split only at consequential decisions, independently accepted prerequisites, deployment/migration/human seams or demonstrated coherence limits. Ordinary internals belong to the coder; consequential scope stays approved. Never merge completed phases.
- **Empirical acceptance and product review.** Name falsifying checks and manual criteria. Discover broadly, batch evidenced blockers, separate optional advice, preserve stable findings; rebase on changed authority, scope, risk or lost continuity. Product primary mode uses advisory reports and at most two passes per stage for recorded cause; delegated mode preserves the role policy’s 600-line/growth/stall/ten-cycle limits. Methodology work requires no independent review.
- **Candidate-bound assurance.** Product identity binds review; full-tree identity binds gate non-mutation and delivery. Declared-authority and reviewed-bookkeeping checks remain independent. Unknown tracked classifications refuse; unknown nonignored untracked paths and `candidate-partition.yaml` itself stay active.
- **Two full gates.** Focused iteration precedes product critique; methodology work is self-checked by the primary. The orchestrator runs the full implementation-candidate sequence ending in `./bin/check all`; accepted close precedes captured status mutation. After all bookkeeping, run the second bare full handoff gate; no tracked write follows success. Bind prospective child completion; parent completion requires separate acceptance.
- **Execution truth.** One append-only trace, exact joins and overlap-safe unions; separately report operator-input parks. Missing measurement is unknown, never zero. Finalize evidence before sanitized offline reports.
- **Safe acceleration.** Use substantial, obvious low-risk time savings within scope, preserving correctness, coverage, determinism, the selected product review contract and both gates. No optimization tangents.
- **Atomic toolchain.** Runtime, metadata, lockfile, setup, focused/full tests, receipts, proofs and callers move together. Use repository wrappers; bad overrides and failed probes never fall back. Keep scratch captures outside the reviewable tree or explicitly ignored.
- **Methodology routing.** All authorized methodology work, including `teach` and `learn`, is one-shot by the primary with commit and fast-forward-push authority after required checks. No delegated planning/coding or independent review/critique. Applies across harnesses and model tiers. Read [review lanes](policies/review-lanes.md).
- **Portable parity.** Canonical sources and thin mirrors; repo-relative committed paths; the declared flat package boundary. Greenfield replacement and delivery/human-judgment boundaries follow the hard rules.

### Activity log (`LOG.md`)

Owning writers append via the deterministic writer at true EOF; preserve bytes and chronology. Parks stay `🚧`; terminal records carry Lessons, evidence, remaining work and truthful outcomes.

### User actions (`user-actions/`)

Glob `user-actions/*.md` at session start; read frontmatter and surface dependencies before work. File human-only actions before ending; use `bin/new-name`, record dependencies/deferral and archive closed entries. Agents close only personally completed actions; GUI, console, pricing and billing checkoff stays human-only.

### Lessons (`lessons/`)

Read both lesson directories before filing/recurring; one row per observation. Run `./bin/lessons validate` and `./bin/lessons candidates`. Human ratification owns graduation/rejection. Harvest process observations and failure analysis at each close/park.

## Universal conventions

Never hard-wrap Markdown prose: one physical line per paragraph, including list-item prose. Preserve syntax-required breaks. Give one executable command per copyable shell fence. Use bare skill names in neutral prose and both harness invocation forms when showing commands. Follow `plain` for operator messages; peers retain full technical fidelity. Use User/operator/owner and they/them in durable role language; authorship credit is separate.

Agent decisions use kickoff’s input-park/`blocked-owner` route; unattended decisions park in artifacts. Only the operator invokes `ask`. Record rulings at their authority and human work in `user-actions/`.

## Glossary

Use the canonical [glossary](briefs/methodology.md#glossary); flag terminology mismatches.

<!-- METHODOLOGY_CONTRACT_END -->
