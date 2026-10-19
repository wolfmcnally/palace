# Activity log

This public log starts with the audited 0.1.0 release tree. Private development records are excluded.

## 2026-09-30 00:56 — METHODOLOGY — Public release preparation

Scope: Public 0.1.0 release preparation authorized by the owner.
Changes: Retained and sanitized methodology; synthetic memory fixtures; standalone public documentation, CI, attribution and packaging; explicit pre-1.0 compatibility posture.
Verification: 268 tests passed; policy-only sequence passed. Fresh frozen assay detected 11/11 historical defects and 11/11 mutants without corpus repair. Exact 0.1.0 wheel installed outside checkout; synthetic index/search/multi-store origins and mismatched-identity refusal passed. Artifacts rebuilt byte-identically; reviewed tree and extracted archives passed redacted Gitleaks.
Remaining: Final complete handoff gate, clean clone/hosted CI, canonical publication and same-path ancestry alignment. No package registry upload or live service operation.

## 2026-09-30 01:17 — METHODOLOGY — Portable contributor shell

Scope: Complete first public release qualification.
Finding: Hosted macOS CI used Apple Bash 3.2, which lacks mapfile; four governed-lane tests refused. The local environment had hidden this prerequisite.
Remedy: Replace mapfile with a portable line-preserving array loop; retain fail-closed selector validation. Both hosted platforms now finish independently.
Verification: All19 existing check/toolchain tests passed under /bin/bash. Final full gate and hosted CI remain required for this new candidate.

## 2026-09-30 01:48 — METHODOLOGY — Observable watcher proof

Scope: Complete release verification without bypassing a refusing gate.
Finding: Tagpush gate exposed a watcher proof that stopped after0.3seconds before native file notification arrived. Its historical name claimed a process signal that it never sent.
Remedy: Synchronize genuine native registration and exact synthetic event delivery; always stop/join, assert daemon return0. Retain selector/family and accurately qualify its contract. Runtime and live stores unchanged.
Verification: Existing10integration tests passed, independent targeted runs passed; unchanged frozen22case assay detected11/11historical and11/11mutants. Finalfullgate and exactcommit hostedCI remain required.

## 2026-10-04 02:55 — END (correction — PARK)
Storage-error follow-up to the audited public source; the release phase remains open.

Scope: Preserve original index/delete transaction failures and investigate vector write amplification. No schema, vector chunk size, shared runtime, production watcher, release or deployment change.

Follow-up route:
- Direct fix — small, diagnostic-determined error-propagation correction; no public API, persistence, security or production ordering change. Native watcher test-only prototypes were discarded after repeatability failures.

Role model/venue:
- Coder and critic: skipped under the eligible direct-fix route; primary self-inspected the final diff. Preflight passed; requested primary astra, observed model unreported. No independent review is claimed for this correction.

Files changed:
- palace/index/core.py — preserve the primary exception; conditional rollback and secondary diagnostic note.
- tests/index/test_core.py — six failure permutations within the retained transaction-atomicity witness; original assertions preserved.
- tests/proof-estate.yaml — strengthen that witness's contract and independent oracle; frozen counts and budgets unchanged.
- lessons/dark-newt.md — physical vector allocation and WAL capacity lesson.
- lessons/courageous-kittiwake.md — early proof-admission validation lesson.
- lessons/esoteric-hawk.md — distinguish exact event delivery from shutdown-flush evidence.
- LOG.md — this truthful parked correction.

Build status:
- Before repair: four of six standalone failure permutations failed by replacing the primary exception.
- Final retained product candidate: 945e06ae07e8337ea0e12b5fc2b23fd6b02f35780166aafa68f7f87ee01c67ae. Exact same candidate passed 33 core/governance focused tests.
- Frozen governance assay: detected 11/11 historical and 11/11 held-out defects; this measures the frozen governance corpus, not every application defect.
- First full gate: failed unadmitted new proof leaves; consolidated regression subcases without increasing budget or removing old assertions.
- Corrected full gate: failed the existing native watcher shutdown witness; 267 tests passed. Subsequent native-readiness prototypes failed bounded repeatability and were discarded to exact baseline bytes.
- Full-gate warnings: six uv messages explicitly ignored the evidence tool's transient VIRTUAL_ENV and selected the repository-managed environment; no runtime adoption is claimed.
- Final structural checks: lint, format, catalogs, lesson validation, log-prefix/chronology and whitespace checks passed.
- Handoff full gate: not run; implementation full gate remains red.

Acceptance:
- Objective: vector allocation control and scratch cleanup passed; original error preservation is focused-test proved. Complete product acceptance and delivery are not claimed.
- Parked for the user: existing release/manual criteria remain open; no release demo or custody action performed.

Findings: Default sqlite-vec 0.1.9 reserves a 16 MiB block for 4096-component vectors at 1024 slots. Two one-vector replacements grew WAL by approximately 16 MiB apiece under a held reader. An 8-slot synthetic control sharply reduced writes; closing the reader allowed truncation. The application format is unchanged. This does not retrospectively identify which readers prevented earlier reclamation.

Delivery:
- Parked: no commit or push while the authoritative full gate is red. No retry-until-green, threshold weakening, tag or release.

Lessons:
- dark-newt filed — budget by physical vector blocks and WAL, not source-file size.
- courageous-kittiwake filed — validate proof admission during focused iteration.
- esoteric-hawk filed — exact event delivery and pending-event flush need separate witnesses; native cause remains unresolved.
- Graduation: none; candidates remain unratified.

Pause reason: Bounded native watcher remediation did not produce repeatable full-gate evidence. The final tree retains only the error-preservation repair, its tests and declaration, and bookkeeping. Private failed runs, discarded prototypes, allocation measurements and patch are retained; all launched commands have terminal results.

Remaining: Qualify native watcher delivery in a separate bounded investigation, obtain green implementation and handoff gates, then deliver. No cloud experiment is authorized. No phase status was advanced.

## END (correction) — Primary-error preservation and causal watcher witnesses

Execution trace: 30d482c84aff42fc8d36e1314891d5c0

Route: direct fix — localized error propagation and test-only witness corrections to previously audited source. Review lane: full; evidence lane: full. No independent role was invoked for this eligible correction; no new review approval is claimed. Release ledger and release acceptance remain unchanged.

Implementation: commit_plan and delete_path preserve the original failure after automatic abort and attach secondary rollback failure as a note. Six failure permutations strengthen the retained transaction witness. Exact-event polling rejects unrelated root replay and uses one two-second deadline measured before notes/alive writes, replacing the previous extending fallback. Shutdown observes the actual burst submission within two seconds, holds the test debounce clock so ordinary polling cannot emit, and verifies the pending record is persisted after daemon termination. Recovery and shutdown-seam selectors use the same exact-target helper with their existing five-second polling budgets. No production watcher, schema or runtime change.

Validation: 43 focused tests passed. Replay-only and expired-deadline controls refused; the exact target control passed. Disabling real shutdown_flush produced the expected missing-persistence failure, with native burst submission and clean daemon termination. The complete implementation candidate gate passed 268 tests, lint, format, typecheck, smoke and all policies. Nine uv warnings indicated ignored inherited temporary VIRTUAL_ENV; the managed repository environment was selected. Frozen estate remains 271 families and 290 leaves; no families added, assertion removed or budget increased. Product candidate: e0b5bde9ec8eb708deddabccf4d84310b5ab766d9330801bb786076af786c05d.

Acceptance: the error-preservation and corrected witness behavior are gate-proved through the direct follow-up route. The intermittent native failure mechanism remains unresolved; earlier failed runs retain their failed outcomes. These results do not qualify all native deliveries or complete release, custody, power-loss or production security criteria.

Delivery: default commit and fast-forward push only after bookkeeping and the second bare full gate; pending at this append. Release phase is not completed by this correction. No tag, release publication, cloud operation or live store mutation.

Ripple: no phase-marker transition or downstream phase rewrite. No DECIDE scope change.

Lessons:
- dark-newt, courageous-kittiwake and esoteric-hawk retained as candidates; no graduation.
- esoteric-hawk occurrence update pending after accepted close: exact-target deadlines and a genuinely pending shutdown record replace misleading witnesses; fault injection proves the shutdown assertion fires.

User demo: N/A — internal error propagation and test proof corrections; release/manual custody criteria remain open.

Remaining: append actual lesson outcome, run bare full handoff gate, then use bin/deliver if green. Investigate a reproduced native failure before claiming a production watcher repair; separately qualify any vector-chunk schema change and broader encrypted-storage/security boundaries.

## 2026-10-04 03:40 — Correction bookkeeping completed

Lesson update completed: esoteric-hawk now has three occurrences and records the falsifying no-flush control. All three retained lessons remain candidates. No phase status or release criterion changed. The implementation candidate gate is accepted; the bare handoff gate and delivery remain pending.

## 2026-10-19 14:20 — END (correction — PARK)
Remote embedding request-budget correction.

Execution trace: 3ada58b2424f417e86992ec8bf0ff8a1

The bounded repair passes its focused regression and all 268 retained tests. Independent code review is blocked before launch, so implementation acceptance, both complete gates and delivery remain pending. The release ledger and historical records are unchanged.

Scope and route: full-cycle follow-up to the audited public embedding source; small change with ordering and public-behavior risk. Inline primary implementation with configured independent code advice. No provider, credential, admission, schema, dependency, fallback, production corpus or deployment changes.

Files changed:
- palace/index/config.py and palace/index/embedder.py — preserve contiguous input/vector order while applying the existing 1024-item cap and a conservative 30000-unit aggregate budget; refuse an oversized individual input before egress.
- palace/index/README.md — document sizing, NFC normalization, unchanged input bytes and conservative refusals.
- tests/index/test_writer_lock.py and tests/proof-estate.yaml — extend the retained request/usage witness and its declaration; no added proof families or leaves, no removed assertions.
- lessons/observant-robin.md — aggregate request context and normalization lesson.
- LOG.md — this parked correction.

Validation: the original splitter failed a synthetic 167-section production plan with 448175 NFC byte symbols in one request. The repaired retained proof passed, including Unicode/NFC expansion, count and size limits, ordering, exact payload digest and chunk-id audit slices, pre-egress oversized-input refusal, and HTTP/provider/dimension failure propagation. Lint, format and proof-estate validation passed (271 families, 290 leaves). The required changed-path selection widened to full because the proof declaration is unmapped; all 268 tests passed in 148.77 seconds. This test duration is the test runner's measurement, not a finalized orchestration duration. Lesson validation passed.

Review: configured primary astra and independent critic opus/high via Claude; provider-observed primary identity unreported. Both real-read preflight probes passed. Automatic approval review then denied the critic watcher launch because the diff and prompt would be disclosed to a third-party model service without explicit end-user authorization for that destination and source disclosure. No reviewer process launched, no independent report exists, and no retry or alternate reviewer route was attempted. The rejected-attempt span records rejection bookkeeping only, not model execution. Exact denial, candidate and resumption inputs remain in local run evidence.

Acceptance: focused behavior is test-proved; independent review, primary acceptance and the implementation and handoff full gates are not complete. Existing release/manual criteria remain open. No live inference or production qualification is claimed.

Delivery: parked — no commit, push, tag or deployment. Source changes remain in the editable checkout and must not be treated as released or qualified.

Lessons:
- observant-robin filed as one local candidate; no graduation.
- Existing candidates retained without modification. No new policy or workflow changes.

Pause reason: explicit source-disclosure approval is needed for the configured external reviewer. The approval-interval recorder also refused the existing nonnumeric release identifier; that interval is unavailable, never recorded as zero. No substitute numeric phase or methodology repair was invented.

Remaining: resolve the reviewer disclosure authorization, resume against the preserved candidate with fresh evidence as required, obtain independent review, reconcile findings, run both complete gates, and deliver only if green. Test protocol: skipped because this correction uses the existing library seam; live consumer validation is separately owned. No phase marker was advanced.

## 2026-10-19 14:39 — END (correction)
Remote embedding request-budget correction: implementation accepted; report, handoff and delivery pending.

Execution trace: 602383ebe5e8415eb201a8118690fb56

The bounded repair now has independent advice, primary dispositions and a green complete implementation gate. It preserves byte-exact inputs, input/vector order, provider identity, no-fallback routing, per-request provenance and failure propagation while bounding aggregate request size. The release ledger and existing release/manual criteria remain unchanged.

Follow-up route: full cycle; inline primary implementation with independent opus/high Claude-subscription advice. The previously denied launch did not execute. New explicit user authorization permitted the same review operation once; subscription authentication, absence of provider overrides and the bounded payload were rechecked. A fresh required evidence lifecycle preserved the original reviewed product candidate and byte-identical prompt. The renewed watcher completed successfully. No alternate reviewer, provider or launch mechanism was used. The critic's Bash permission was denied; it reviewed source and tokenizer evidence, while the orchestrator ran executable checks.

Files changed:
- palace/index/config.py and palace/index/embedder.py — apply the existing 1024-input cap and conservative 30000-unit aggregate bound, prevalidate oversized inputs, and advance batches by construction.
- palace/index/README.md — explain NFC/raw-byte sizing, model-wide applicability, conservative refusals and dated relayed provider observations, without claiming an exact measured threshold or revised throughput.
- tests/index/test_writer_lock.py and tests/proof-estate.yaml — strengthen the retained proof with a synthetic 167-section production plan, Unicode expansion/contraction, count and exact-size boundaries, order, audit and failure cases; unchanged 271 families and 290 leaves.
- lessons/observant-robin.md — retained aggregate-context lesson; review-resolution note pending after this accepted close.
- EXECUTION_LOG.jsonl and LOG.md — truthful finalized evidence and append-only correction records.

Review: the critic confirmed the pinned tokenizer bound, order, audit and refusals. All four advisory findings were adopted: preserve dated provider evidence and its limits; prove raw-byte sizing when NFC shrinks; make batch progress local; clarify every OpenRouter model inherits the bound. The primary assessed these narrow post-review changes and recorded verification. Reviewed candidate: 90e97332135c4953ad0ae62f28c09a2e55ece5dbab151351d564a85ec3d6215c. Reconciled product candidate: 06f97b35615462d56882ed9ec90b98391b73bee99c83ba5150b26ae224fd4932.

Build status:
- Revised retained regression: 1 passed.
- Changed-path selection widened to full: 268 passed in 147.76 seconds.
- Command zero: passed.
- Complete implementation gate: lint, format, typecheck, all 268 tests (148.65 seconds), CLI smoke and every policy check passed; full-tree candidate unchanged at 882baa312cd58c5c5cae518e4bd5d8c45d7bc0a191ff97b43a0dce6cda2786de.
- Diagnostic review: no repository-check warnings; one evidence-wrapper warning because an optional artifact path was absent. The complete wrapper output and digest-bound full-gate receipt/log remain preserved; the optional gate digest is explicitly null.
- Acceptance evidence: validated. Handoff gate runs only after all required close artifacts; completion remains contingent on its receipt.

Acceptance:
- Objective: bounded contiguous batches, exact payload/vector order, per-request audit, pre-egress oversized-input refusal and unchanged remote refusal behavior are independently advised, primary-dispositioned and gate-proved.
- Parked for owner: live published-corpus qualification, product/readability judgment and existing release/custody/manual criteria. No production or deployment operation was performed by this correction.

Delivery: default commit and fast-forward push only after the required report and bare handoff gate; both remain pending. No commit, push or release is claimed.

Ripple: no phase status or downstream scope change. No new feature, provider, credential, admission, dependency or workflow change.

Lessons:
- observant-robin retained as one local candidate; review-resolution note pending. No graduation.
- Existing esoteric-hawk remains a three-occurrence candidate already awaiting human ratification; no rule graduation is part of this correction.
- Timeout recommendation: no group has the required 30 successful samples; legacy malformed/incomplete records were ignored by the recommender. No configuration changed.

Awaiting user input: the earlier source-disclosure wait is resolved. Its numeric-only park recorder had refused the declared release identifier, so elapsed input-wait time is unavailable, never zero. This correction does not invent a phase identifier or change report machinery.

Remaining: append actual lesson outcome, generate the required report under the declared release identity, then run the bare handoff gate and deliver only if green. Test protocol: skipped because the new invariants are enforced by the retained automated library proof; live qualification is separately owned. The broader release remains incomplete.

Additional qualification relayed by the owning operator after source freeze: the existing published 167-chunk page completed in five successful requests with 31,827 prompt tokens and zero failed requests. The index build and identity checker exited zero; provider/model/dimension remained OpenRouter/DeepInfra, qwen/qwen3-embedding-8b, 4096. This was separately executed and is attributed evidence, not a run by this correction. Publication remains pending.

### Execution timing

Execution trace: 602383ebe5e8415eb201a8118690fb56
- Outcome: `success`
- Makespan: 827531064542 ns (827.531064542 s)
- Intelligence: 199905005458 ns (199.905005458 s)
- Gates: 153630350292 ns (153.630350292 s)
- Orchestration: 769562081042 ns (769.562081042 s)
- Wait: 199468412500 ns (199.468412500 s)
- Retry/rework: 0 ns (0.000000000 s)
- Failed work: 0 ns (0.000000000 s)
- Unattributed: 57968983500 ns (57.968983500 s)
- Category totals are interval unions and may overlap.

#### Slowest material spans

- `orchestration.implementation` — category `reconciliation`, attempt 1, outcome `success`: 471547579167 ns (471.547579167 s)
- `orchestration.acceptance` — category `reconciliation`, attempt 1, outcome `success`: 219633015625 ns (219.633015625 s)
- `role.code-review` — category `intelligence`, attempt 1, outcome `success`: 199905005458 ns (199.905005458 s)
- `role.code-review` — category `wait`, attempt 1, outcome `success`: 199468412500 ns (199.468412500 s)
- `gate.implementation` — category `gate`, attempt 1, outcome `success`: 153630350292 ns (153.630350292 s)

## 2026-10-19 14:40 — Correction bookkeeping completed

The observant-robin review-resolution note is now written and validates. The finalized execution trace, primary dispositions and full implementation-gate evidence validate. Operator-park summary reports no recorded open interval; the earlier rejected recording remains unavailable, not measured zero. Product candidate remains 06f97b35615462d56882ed9ec90b98391b73bee99c83ba5150b26ae224fd4932. Required report generation, the bare handoff gate and delivery are pending; no release marker changed. The log writer refused its automatically synthesized local-time heading because earlier blocks used UTC; no append occurred. This entry uses the actual current UTC time consistently.

## 2026-10-19 14:41 — END (correction — PARK)
Remote embedding request-budget correction: delivery parked after implementation acceptance.

Execution trace: 602383ebe5e8415eb201a8118690fb56

The complete implementation gate passed and its independent advice, primary dispositions and timing evidence validate. The required execution report attempted once under the declared `release` identity exited 2: `execution-telemetry: accepted trace is absent or belongs to another phase`. The finalized root is present with operation `phase.release`; the report projector excludes nonnumeric phase identifiers before selecting the accepted trace. No report was generated. This later close failure does not rewrite the successful implementation trace or its accepted closure.

Completed: revised retained proof; changed-path full selection (268 passed); complete implementation gate (268 passed plus lint, format, types, smoke and policies); independent configured opus/high Claude-subscription review; all four advisory dispositions; finalized evidence; unchanged product candidate 06f97b35615462d56882ed9ec90b98391b73bee99c83ba5150b26ae224fd4932. Separately relayed live qualification of the existing published 167-chunk page and identity checker passed; no publication is claimed.

Delivery: parked. The bare handoff gate, commit, push and deployment did not run. No report omission, alternate identifier, provider route, schema change, validation weakening or machinery repair was attempted. All launched processes have terminal results; the source candidate remains intact.

Lessons:
- observant-robin retained with review-resolution evidence.
- caped-bonobo filed as a methodology candidate: phase identity must remain admissible across required close consumers. No graduation or policy change.

Pause reason: mandatory report generation refused the repository's declared phase identity. The failure signature and exact command/output are preserved in local evidence; parent-directed separate authorization is required before any machinery correction. Existing release/manual criteria and plan state remain unchanged.

Remaining: resolve the phase-identity reporting dependency under separate scope, produce the required report, complete all needed unchanged gates, and deliver through the repository guard. Preserve the current implementation acceptance and original denied-review evidence. Test protocol: skipped because the behavior is covered by the retained automated library proof; live qualification was separately owned. No repeated reviewer launch is needed merely because report generation refused.

## 2026-10-19 15:05 — END (bounded correction clarification)
Remote embedding request-budget repair remains a bounded fix; the earlier release-phase binding is superseded for this task.

The operator explicitly clarified: “that should just be a bounded fix and not full phase work in any case.” The complete original instruction is preserved in local evidence; committed wording omits consuming-project identity. The separate bookkeeping authorization states: “Authorize bounded corrections to agents’ own uncommitted development bookkeeping when evidence shows convergence on the user's agreed goal, preserving rejected originals and valid history and rerunning unchanged validation. Ask if evidence shows runaway scope divergence. Excludes expanded permissions, weakened safeguards, permanent data loss, and unrelated work.” This is a task-scoped clarification, not a permanent policy amendment.

Assessment: the repair was attached to plan/phase-release.md without establishing a matching public-release deliverable or prior accepted phase critique. That phase concerns public packaging, documentation, tags and release qualification. The correction brief and actual changes concern remote request capacity. The public audited-tree statement and an earlier generic correction did not establish the missing phase continuity. The agent's use of the sole release row was unsupported. The standalone new-files-only doctrine route is not claimed for this existing-code repair; the operator's explicit bounded-task classification governs this close.

The original traces, review denial, report refusal, accepted implementation closure, log entries and failure signatures remain intact. The failed dashboard command really refused; its outcome is not converted to success. Its inferred ownership of this task is superseded. No report generator, phase identifier, plan, policy, schema, safeguard or permission changed. Genuine phase reporting requirements remain in force for phase work.

Implementation and assurance remain unchanged: product candidate 06f97b35615462d56882ed9ec90b98391b73bee99c83ba5150b26ae224fd4932; independent configured code advice with all four findings dispositioned; focused regression and changed-path full selection passed; full implementation gate passed 268 tests plus lint, format, types, smoke and policies. The optional diagnostic artifact remains absent and its digest null, explicitly reconciled to the actual complete wrapper output and receipt-bound log. Separately relayed live larger-page qualification and identity verification passed. The observed defect is insufficient aggregate request bounding; no evidence establishes drift since a previous checkpoint.

Files changed since implementation acceptance: only the agent's append-only LOG and candidate lesson clarification. Original bookkeeping bytes are preserved locally by digest. Product, test and declaration bytes remain frozen. Existing release plan/history/manual criteria remain unchanged.

Acceptance: bounded batching behavior is independently advised, primary-dispositioned and implementation-gate proved. Product/readability judgment and broader release/custody criteria stay with the operator. No publication is claimed.

Delivery: run the unchanged bare ./bin/check all after this final bookkeeping. Only a green receipt for the actual complete tree permits ./bin/deliver with every exact changed path, an ordinary factual commit and fast-forward push to the existing unambiguous upstream. No tracked write follows a successful handoff gate. No commit or push is claimed at this append.

Lessons:
- observant-robin retained: bound request aggregate size independently of input count and preserve normalization-aware sizing.
- caped-bonobo superseding assessment: verify that a task belongs to a phase before importing its lifecycle; the prior proposed generator remedy is not a prerequisite for this bounded fix. Candidate only, no graduation.
- Existing lessons and history preserved; no permanent rule change.

Remaining: unchanged full handoff gate and guarded delivery. Test protocol: automated retained library proof plus separately owned live qualification; no new phase demo commissioned. The broader public release remains incomplete.

## 2026-10-19 15:09 — Owner classification clarification

The owner classifies the repaired defect as a drift regression after a previously working consuming checkpoint workflow and intervening Palace extensions. No specific causal commit or mechanism has been isolated. This attribution supersedes the earlier unqualified statement that drift was not established; the distinction between owner classification and technical causal attribution is retained. Palace remains greenfield. No maturity, compatibility, stabilization or release-status change is authorized.

This clarification arrived while the prior unchanged handoff gate was running. That gate subsequently passed all checks, including 268 tests, on complete-tree candidate 462f4cac72ecd1c5429812620261e8591f07bacf658f0ddf139f2d66a9d771d5. Its successful receipt is preserved. This newly authorized bookkeeping append reopens the handoff tree, so delivery requires a fresh unchanged bare full gate after this entry. Product candidate remains 06f97b35615462d56882ed9ec90b98391b73bee99c83ba5150b26ae224fd4932; independent advice, primary dispositions and implementation gate remain unchanged. No code, test, policy, report machinery or plan was modified.

Delivery remains pending the fresh full handoff receipt and the existing exact-path bin/deliver guard. No tracked writes are planned after that final success. Earlier rejected-path evidence and valid history are preserved.
