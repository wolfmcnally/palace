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

## 2026-10-20 15:13 — START
Bounded indexing and recovery

Execution trace: 9d9837630034446ead8d2f9fc9e80743
Baseline: 6c244dc6498ad101fa3a245802635c7c4588256d — preserve chunk, embedding input and provenance semantics

Planned work:
- A bounded fair chunk scheduler for synchronous builds, with deterministic folding and completed-document publication across selected roots.
- Private durable receipts bound to source snapshots, exact embedding inputs and complete embedding identity, with bounded validated recovery.
- Declared admission/resource limits, visible oversized-source refusal and structured progress without changing chunk, embedding or provenance semantics.
- A separate prompt daemon first-error stop/park with safe shutdown and a checkpoint before the failed event.
- Fake/mocked-provider qualification within the existing proof inventory, updated operations documentation and attributable guarded delivery.

## 2026-10-20 15:24 — PARK
Bounded indexing and recovery — named identity preparation

Execution trace: 9d9837630034446ead8d2f9fc9e80743

The initial named phase reached plan capture but no independent review process launched: native approval review denied two prelaunch requests. Public-source visibility was verified between them; the second denial specifically required authority for the private derived plan/prompt and Claude destination. Both original requests, captured plan and refusal observations remain preserved. The numeric-only operator-park recorder also refused this admitted named phase; awaiting-input duration is unavailable, never zero. Product source, tests and policy remain unchanged.

The operator has now explicitly approved the exact plan/prompts/change-summary/diff egress through their Claude subscription, excluding secrets, personal data, production documents and unrelated runtime state; attributable relay is Folded request `0c818a29ccbe4a359e05b14a608fe39d`. That relay also authorizes a new unused valid numeric phase identity with an explicit correction mapping. A fresh trace/capture/review will use that identity; this run remains parked, never retroactively successful. The failure-record park identifier identifies this diagnostic close, not a fabricated operator-wait interval.

Lessons:
- Existing `caped-bonobo` already records named-phase admission versus downstream numeric-only lifecycle refusal. This occurrence confirms the gap; no checker or policy change is authorized. Preserve the old run and use the operator-approved explicit phase mapping with fresh capture.
- The live-Ollama smoke was incorrectly described as fake-only; a separate scratch correction removes it without changing the original captured plan. No live inference occurred.

Remaining:
- Fresh numeric phase, corrected plan capture, independent plan/code advice, implementation, fake-only qualification, both full gates and guarded delivery. Manual product/demo judgments remain open.

## 2026-10-20 15:26 — START (resumed)
Phase 20 — Bounded indexing and recovery

Execution trace: cd78761fae9b422f9a10210816d0e7fd
Baseline: 6c244dc6498ad101fa3a245802635c7c4588256d — preserve chunk, embedding input and provenance semantics

Identity correction: the initial `bounded-indexing` trace closed parked, preserving its captures and two unlaunched native review denials. Attributable operator relay `0c818a29ccbe4a359e05b14a608fe39d` authorizes Phase 20 as the next unused numeric identity and one reviewed resubmission under the new exact derived-material/Claude-subscription approval. This run has fresh authority capture and no carried successful review or reset consumed review budget. The corrected fake-only plan excludes live Ollama; full-gate smoke is the CLI version check only.

Planned work:
- A bounded fair chunk scheduler for synchronous builds, with deterministic folding and completed-document publication across selected roots.
- Private durable receipts bound to source snapshots, exact embedding inputs and complete embedding identity, with bounded validated recovery.
- Declared admission/resource limits, visible oversized-source refusal and structured progress without changing chunk, embedding or provenance semantics.
- A separate prompt daemon first-error stop/park with safe shutdown and a checkpoint before the failed event.
- Fake/mocked-provider qualification within the existing proof inventory, updated operations documentation and attributable guarded delivery.

## 2026-10-20 15:27 — PARK
Bounded indexing and recovery — numeric identity correction

Execution trace: cd78761fae9b422f9a10210816d0e7fd

The explicit derived-material/Claude-subscription approval passed native review, but the watcher stopped before creating a Claude process: Phase 20 already had historical advisory-budget state. The initial unused-ID check covered retained phase files and run metadata but omitted the phase-hashed budget filenames. No prior budget was reset, no review ran, and this task has no successful advice to carry. The original Phase 20 sketch and captured fake-only plan remain unchanged as evidence of this mistaken selection.

Reading the authoritative budget-key definition and enumerating the existing metadata/hashed keys establishes numeric IDs 16 through 20 as used; Phase 21 is the next unused identity. The existing operator instruction to select the next unused valid numeric ID covers that correction. A fresh Phase 21 capture will preserve the complete named-entry → Phase 20 → Phase 21 mapping. The watcher already recorded its rejected dispatch and error intelligence span without a wait span; no provider pass was consumed for this task.

Lessons:
- Check the actual phase-budget key function and all independent identity inventories before declaring an identifier unused. File/run listings alone are incomplete when budgets are hashed independently. This is a provisional local observation; no machinery or policy is changed.
- All earlier denials and original artifacts remain preserved. No provider switch, live inference, product mutation, budget reset or fabricated review history occurred.

Remaining:
- Fresh Phase 21 capture and the bounded prelaunch-corrected review submission, then approved implementation, qualification, both gates and guarded delivery. Manual product/demo judgment remains open.

## 2026-10-20 15:29 — START (resumed)
Phase 21 — Bounded indexing and recovery

Execution trace: 2dea893118ec463bada791eac2ff5704
Baseline: 6c244dc6498ad101fa3a245802635c7c4588256d — preserve chunk, embedding input and provenance semantics

Identity correction: named capture and mistaken Phase 20 capture remain truthfully parked and immutable. Source-defined hashed budget inventory proves IDs 16–20 used; Phase 21 is unused. Operator relay 0c818a29ccbe4a359e05b14a608fe39d authorizes this mapping and exact Claude subscription review egress. Historical budgets are unchanged; no reviewer has run for this task. The new submission is the bounded correction of the prelaunch ID collision, not another provider pass.

Planned work:
- A bounded fair chunk scheduler for synchronous builds, with deterministic folding and completed-document publication across selected roots.
- Private durable receipts bound to source snapshots, exact embedding inputs and complete embedding identity, with bounded validated recovery.
- Declared admission/resource limits, visible oversized-source refusal and structured progress without changing chunk, embedding or provenance semantics.
- A separate prompt daemon first-error stop/park with safe shutdown and a checkpoint before the failed event.
- Fake/mocked-provider qualification within the existing proof inventory, updated operations documentation and attributable guarded delivery.

## 2026-10-20 16:34 — END
Phase 21: Bounded indexing and recovery

Small documents now publish before earlier giants finish, complete embedding turns survive interruption, and the daemon stops at its first failed event. The independently reviewed implementation passed the complete candidate-bound gate. Status, lesson/action filing and the tracked report are pending below; the final handoff gate and guarded delivery have not yet run.

Files changed:
- palace/index/scheduler.py and staging.py — finite multi-root turns, private source/identity-bound receipts, hard admission limits, honest progress and atomic whole-document publication.
- palace/index/build.py, core.py, config.py, embedder.py and __init__.py — integrate scheduling while preserving parser/enrichment, exact provider inputs, provenance, usage ownership and existing public build surfaces; remove the obsolete soft bound.
- palace/index/_errors.py, schema.py, writer.py, server.py, tail.py, daemon_state.py and cli.py — typed identity failures and separate terminal work-failure parking, queue/tail wake and cursor custody.
- palace/reindex/bootstrap.py — bounded collection before manifest admission.
- tests/index/test_core.py, test_scheduler.py and test_index_server.py — retain original reconstruction fixtures/assertions through four compensations and qualify four independent bounded-indexing failure families.
- tests/proof-estate.yaml and reports/test-governance/palace-reset.jsonl — compensated admissions, unchanged frozen counts and corpus.
- palace/index/README.md and bin/README.md — supported envelope, durable resume, refusal/stale-row effects and truthful manual recovery.
- plan/INDEX.md and the preserved named/20/21 sketches — explicit commissioned identity correction, preserving the separate release phase and original records.
- LOG.md and EXECUTION_LOG.jsonl — append-only history and current execution evidence. Lessons, manual review action and reports/execution output remain pending bookkeeping.

Build status:
- Focused qualification: 48 fake/mocked-provider tests pass; lint, format and type checking pass.
- Managed implementation sequence: focused 48 tests, proof-estate validation, then ./bin/check all — all pass on unchanged product/full-tree identities. Full gate: 268 tests plus CLI version smoke and every policy check pass.
- Final frozen assay: all 22 historical/held-out cases detected by parsing every row of proof-assay-final.jsonl; frozen corpus/patches unchanged.
- Four intended negative controls failed at their property assertions, and exact original source bytes were restored; metadata and diagnostics retained outside the checkout.
- Diagnostics: two wrapper notices in each focused/governance gate, nine in the full managed invocation (eight in its complete captured log plus one parent notice). Each is the same uv temporary VIRTUAL_ENV mismatch; canonical wrappers ignored it and selected the locked repository environment. No product/test warning or error remained. No interpreter/cache/global-tooling switch was used.
- Handoff gate: pending a second bare ./bin/check all after all tracked close writes. Completion and delivery remain contingent on its ignored receipt.

Review lane: full, primary advisory authority. Evidence lane: full. Follow-up route: initial commissioned implementation, with bounded primary corrections after code advice; no delegated writer.

Role model/venue — orchestrated by codex:
- Planner and coder: inline primary, requested astra; harness/model/effort observations unavailable from a separately qualified invocation, not copied from requested values. No provider fallback.
- reviewer: requested model=opus effort=high venue=claude; harness_version=2.1.305, observed_model=claude-opus-5-5, observed_effort=unreported; observation_errors=[]; duration=323.969498583 s, first event=0.616 s, longest idle=67.99 s; success.
- critic: requested model=opus effort=high venue=claude; harness_version=2.1.305, observed_model=claude-opus-5-5, observed_effort=unreported; observation_errors=[]; duration=386.838737833 s, first event=0.583 s, longest idle=46.341 s; success.
- The earlier named phase and mistaken Phase 20 runs remain truthfully parked. Both native egress denials preceded process launch. Explicit attributable operator permission subsequently covered the plan, prompts, summaries and code diff through the existing Claude subscription; the first admitted Phase 20 watcher then refused before provider creation on its historical budget. Phase 21 was verified unused through the source-defined hashed budget key. No history, authority hash or budget was reset.

Execution timing:
Execution trace: 2dea893118ec463bada791eac2ff5704
- Current numeric run makespan: 3810.153170250 s; intelligence union: 710.808236416 s; gate union: 210.220401749 s; orchestration union: 3802.398793376 s; wait union: 709.922724375 s; unattributed: 7.754376874 s. Category unions can overlap and are not summed as elapsed time.
- Managed failed/retry span time: 0.000000000 s/0.000000000 s. This does not relabel raw iteration/static failures or historical parked attempts as successful measured work. Their evidence is preserved separately; historical operator-wait duration remains unavailable.
- Current Phase 21 operator-input summary has no intervals and no open park. Earlier identities are separate preserved histories, not zero-duration waits on this trace.
- Timing validation: finalized exact monotonic spans and role/gate joins pass; no telemetry_incomplete entries. Exclusive work/peak concurrency not supplied by this primary summary and are not invented.

Candidate-bound evidence:
- Product implementation: fa6dbcf2bc3a99d8902e9a9d322b6ca5b0e5984111b6c6fa882bb57ac7a9be64; full implementation tree: 9f65b1a9fc5f1d69202cfcd40a2928733b8c519968708a0cad92e2e0d798ce26. Each matched its own before/after capture.
- Independent reports: plan-1 has 13 findings, code-1 has 10; all 23 have explicit primary dispositions (21 adopt, 2 decline). Declines retain fixed invocation source snapshots and visible corrupt-receipt inspection. Partial suggestions outside the selected contract are explained in the primary decision. One advisory pass per stage, no synthetic approval verdict.
- Three final managed gates, all diagnostics reviewed. Immutable gate manifest: 2d65ea6cf6be996187c08afbb9ed9cae2d92a8cec9a089266281894ba11a2d14. Acceptance evidence validation and timing summaries pass.

Wall-clock observations:
- Independent code advice exposed a per-publication full staging rescan. Incremental byte accounting removes that quadratic filesystem cost while preserving reservations, receipt validation and both gates. Focused proofs preceded the full suite; no assurance was skipped.

Acceptance:
- Objective: bounded fair finite service, reserved small-file admission, source/full-identity receipt binding, interruption/process-death recovery, atomic searchable publication, exact provider/usage/provenance slices, explicit unsupported input/resource refusal, truthful progress, and daemon last-success cursor/failure parking are independently reviewed and candidate-gate proved. Existing chunk/enrichment/search/copy semantics remain owned by their established implementation/tests.
- Parked for the user: product/readability judgments and the User Demo. A disposable protocol was mechanically qualified with fakes; that is not human acceptance. Logical staging/source limits are engineering defaults, not measured RSS, physical allocation, WAL or monetary bounds. Source edits can refuse even before deferred admission; a rerun replans and reuses compatible receipts. No automatic corruption repair, daemon retry or online identity swap is claimed.

Delivery:
- Default guarded commit and fast-forward push only after the final handoff gate. Public source publication through the existing upstream was disclosed; no release/tag/package deployment or production mutation is included. No paid inference, production documents/stores, grants, credentials or service restart were used. The original proposal-only restriction was superseded only for the explicitly approved implementation scope.

Ripple:
- AUTO: none required; separate public release preparation and its existing next marker remain unchanged. No subsequent product phase is commissioned by this run. Its named identity is not rewritten for report rendering.
- DECIDE: none blocking this bounded scope; no policy or broader product change is inferred from advice.

Lessons:
- Pending occurrence: caped-bonobo — named identity admitted by initialization but refused by later lifecycle surfaces; append one Phase 21 observation while preserving the prior diagnosis/history.
- Pending filing: real-dog — inspect the actual persistent key/occupancy before selecting a fresh phase; methodology candidate, one observation.
- Existing graduation recommendation: esoteric-hawk has three occurrences and remains a human decision; no graduation or policy edit is performed by this phase.
- Recalibration: recommend-timeouts reports no target with the required 30 successful samples; legacy incomplete/malformed records are excluded with preserved diagnostics. No timeout configuration changes.

User demo:
User Demo: With disposable sources and a fake provider, inspect progress showing a later small document committed while an earlier large one remains pending; interrupt and resume to see validated receipt reuse. Inspect oversized-source refusal and the daemon's failed-event park. Product/readability judgment remains with the operator; do not use production stores or paid providers for this demo.

The concrete disposable fake-provider commands will be filed in user-actions/pygmy-quoll.md after this accepted close. The protocol shows durable reuse, small-before-large publication and oversized refusal; the actual daemon witness checks idle/full queue parking and the exact successful-prefix checkpoint. No production restart is requested.

Remaining:
- Apply the prepared ledger transition, file/validate lessons and manual action, generate the sanitized offline report, run the second bare full gate, then use the guarded explicit-path delivery. These are pending at this append-only accepted-close boundary.
- Human product/readability review remains open after delivery; separate release work is unchanged and outside this task.

## 2026-10-20 16:35 — Phase 21 close bookkeeping

Phase 21 accepted-close bookkeeping landed: the exact prospective ledger was applied and verified; the separate release row and all original sketches/history remain unchanged. Filed lessons/real-dog.md with one methodology observation, added one distinct caped-bonobo occurrence (two total), and filed user-actions/pygmy-quoll.md for human product/readability review with mechanically qualified disposable fake-provider commands. Lessons validation and append-only log checks pass. Existing esoteric-hawk remains a three-occurrence human graduation recommendation; no policy change was made.

Ripple requires no downstream edit. The sanitized offline dashboard is the next and final tracked close write; report generation and the second bare full handoff gate are still pending at this append. No source or test changes followed candidate acceptance. Guarded explicit-path delivery follows only after the handoff receipt; product/readability acceptance remains with the user.

## 2026-10-21 09:49 — START (resumed)
Public release preparation — next version 0.2.0

Execution trace: 2f811aa69ab249c2b0df02c6b5d8ebdc
Baseline: 65ec2cad58c0956187c4e276bca6e022410f844f — preserve the historical v0.1.0 tag/assets and existing source semantics.

Planned work:
- Standalone quickstarts, deeper reference guides, contribution and security instructions.
- Third-party attribution, pinned CI, fresh full gates and frozen mutation assay.
- Exact package build, clean install and synthetic retrieval smoke.

The operator explicitly approved completion through new-version tagging, existing-public-repository publication and anonymous downloaded-artifact verification, not readiness alone. Prepared version and release-authority updates remain unaccepted pending fresh independent review and all gates. This continues the existing release phase, preserving all prior records and review allowance history. No package-registry upload, new grants, production mutation or inference is included.

## 2026-10-21 09:49 — PARK
Public release preparation

Execution trace: 2f811aa69ab249c2b0df02c6b5d8ebdc
The independent plan report failed ingestion because affected_paths included its absolute scratch plan path. The original response and diagnostics remain immutable; no report is relabeled as admitted. Initial final-version builds were identical and both clean platform installs passed, but these remain unaccepted prior evidence. No release/tag or remote source mutation occurred.

Diagnosed resume: a fresh trace will capture bounded release-prose corrections and obtain the second and final plan-advice pass with explicit repository-relative output labels. Historical phase allowance is retained; the exact path-schema diagnosis consumes one of three self-resume units, leaving two. Same provider/model/effort, no approval bypass. The role and all qualification commands terminated; no child process remains active. Candidate lineage is the prepared current working tree, without restoration or discarded changes.

The START header was mistakenly typed as 09:49; the setup trace establishes it was appended at approximately 09:43 local time. That incorrect original header is preserved; no exact timing claim derives from it.

Lessons:
- Already covered: inspect enforcing contracts before constructing invocations; the child-span scope flags, pinned tool layout and required definitions table were corrected after their exact diagnostics. No new permanent rule is needed.
- Report path vocabulary must be explicit when reviewing private scratch plans. Preserve raw outputs and correct through fresh admitted reviewer output, not by silently rewriting the original. Candidate lesson will be harvested at close.

## 2026-10-21 09:51 — START (resumed)
Public release preparation — final-version qualification

Execution trace: 0ed21a3e40454afdb5ffc97b4e34d8c0

Planned work:
- Standalone quickstarts, deeper reference guides, contribution and security instructions.
- Third-party attribution, pinned CI, fresh full gates and frozen mutation assay.
- Exact package build, clean install and synthetic retrieval smoke.

The schema-refused original review run is parked and immutable. Fresh authority capture includes only bounded approved release-prose corrections. The second and final plan-advice pass retains the same existing phase, provider, model, effort and cumulative budget. All initial builds/installations and successful historical-wheel upgrade smoke remain prior evidence until final changed-package qualification. Existing code-advice pass 1 belonged to the historical misbound remote-budget repair; the remaining pass is used for the complete current release candidate. No report omissions, invented phase or machinery modifications are authorized.

## 2026-10-21 10:11 — PARK
Public release preparation — 0.2.0 artifacts qualified, evidence recovery unresolved

Execution trace: 0ed21a3e40454afdb5ffc97b4e34d8c0

Version 0.2.0 metadata, root-only lock update, version-bound CI/CLI smoke, release and upgrade documentation are prepared. No commit, push, new tag or release was made. Existing v0.1.0 history and assets remain intact; their tag and anonymous asset-hash baseline is retained privately. The operator's whole-release approval remains applicable and is not being requested again.

Verified preparation:
- Both final-version builds are byte-identical; every archive member, generated metadata, wheel RECORD, license, dependency declaration and packaged source byte was inspected.
- Fresh exact-wheel installations on macOS ARM64 and Linux ARM64 passed installed-byte, CLI, injected indexing/search/reranking, no-op, recorded identity, origin-preservation and mismatch-refusal checks without provider calls.
- The anonymously downloaded published 0.1.0 wheel built a deterministic synthetic store; the hash-bound final wheel searched it and performed an unchanged no-op while preserving its source bytes. This does not certify real providers or arbitrary stores.
- Fresh frozen assay detected all 22 cases. Complete corrected ./bin/check all passed all 268 tests, lint, format, typecheck, version smoke and policies, with no warnings. The first full run's single old-version fixture failure is preserved; its existing escaped expectation was corrected and its focused check passed before the full rerun. No new product test or proof family was added.
- Independent Opus/high plan and code reports are preserved and ingested. Their advice prompted precise upgrade limits and source/artifact custody checks. The final small changelog/test corrections have primary dispositions and refreshed qualification, not a fabricated third review. Independent disclosure review found no actual secret or consumer identifier; bounded content screens found none, but neither establishes universal absence. Gitleaks was unavailable and its old-release result is not reused as current proof.

The normal evidence validator refuses the run because the orchestrator used phase-wide review pass 2 as its first local attempt number. Closed telemetry and registrations retain that exact observation. The supported carry-advice route requires the same refused source validation. No missing attempt is invented, no historical record is rewritten, and no exhausted allowance is reset. This differs from the earlier report-path failure, whose parked original remains immutable. No managed implementation acceptance, completed phase, handoff gate or delivery is claimed.

Candidate lineage: the independently reviewed candidate received only documented changelog precision and an existing CLI version-fixture correction, plus truthful bookkeeping. Final package qualification was rebuilt after packaged prose changes. The final source gate preceded this lesson/park/finalized-trace bookkeeping; a new normal handoff gate remains required. Quickstarts and attribution were preexisting release preparation, not rewritten for this minor version. The phase-file user review and existing readability/provider walkthroughs remain human judgments; package plumbing was exercised automatically.

Remaining: supported disposition of the exact local-attempt evidence seam, then normal accepted close/handoff/delivery, hosted branch CI, pristine delivered-commit build comparison, new tag and successful tag CI, GitHub release publication and anonymous downloaded-new-artifact verification. No package registry, live store, inference, security/grant change or service operation is included. Release notes are prepared from the changelog plus hashes, excluding coordination details. No unrelated phase is selected.

Lessons:
- Filed magenta-rottweiler: distinguish per-run role attempts from phase-wide advisory passes; candidate methodology/bin observation, not authority to change the checker.
- Existing fixture-inventory rule already covers the missed escaped version literal; both plain and escaped forms were searched after correction.
- Existing esoteric-hawk remains a three-occurrence human graduation candidate; no rule was graduated. Other already-reported product/readability judgments remain open and are not new publication prerequisites.

## 2026-10-21 10:58 — START (resumed)
Public release preparation — qualified same-phase evidence recovery

Execution trace: 79e3b6a6252b4419a9e0a831e2013ddf

Planned work:
- Standalone quickstarts, deeper reference guides, contribution and security instructions.
- Third-party attribution, pinned CI, fresh full gates and frozen mutation assay.
- Exact package build, clean install and synthetic retrieval smoke.

The retained tooling owner supplied a narrowly adapted, independently reviewed and gate-proved correction. Only bin/kickoff-evidence and the passive budget-validation closure in lib/agentic_starter/advisory.py were adopted with exact preimage/output hashes. Original tool bytes, patch, source review/native evidence and recipient refusal were archived privately. Original 24 evidence hashes and unchanged two-plan/two-code phase ledger were checked before adoption. No old pinned tools or closed trace was edited.

This genuine same-release primary full/full continuation attests the two independently preserved predecessor runs and carries only final plan-2/code-2 reports. No new role/model invocation, fake local attempt, budget exception/reset, schema weakening or new phase is authorized. Existing final package/platform/upgrade evidence is retained only after checking its actual inputs; no replay is claimed. The current scope still includes guarded delivery, exact branch/tag CI, new-version GitHub release and anonymous downloaded-artifact verification under the original whole-release approval.

## 2026-10-21 11:10 — END
Public release preparation — source and package qualification

Source preparation is accepted on the evidence below. The complete release remains in progress: guarded delivery, hosted branch/tag CI, pristine delivered-commit artifact equality, publication and anonymous downloaded-artifact verification follow. No new release or tag is claimed by this record. The release row stays in progress until those objective criteria are actually verified; no new phase is selected.

Execution trace: 79e3b6a6252b4419a9e0a831e2013ddf

Files changed:
- pyproject.toml and uv.lock — version 0.2.0, with no runtime dependency change.
- .github/package-smoke.py, .github/workflows/ci.yml and tests/test_smoke.py — final-version artifact/CLI witnesses and managed build command.
- CHANGELOG.md, README.md and RELEASE.md — minor-version scope, exact admission/upgrade/caller changes and package instructions; historical 0.1.0 retained.
- plan/INDEX.md, plan/phase-release.md and briefs/deployed-consumer-readiness.md — existing release scope made version-neutral; original authorities preserved in history.
- bin/kickoff-evidence and lib/agentic_starter/advisory.py — exact independently qualified selective recovery closure; no producer, cancellation, ordinary two-pass or close change.
- LOG.md, EXECUTION_LOG.jsonl and lessons/magenta-rottweiler.md — truthful failures, successful recovery and candidate learning.

Build status:
- Exact archive/metadata/source/RECORD inspection and two-build equality: OK; final package inputs rechecked unchanged after tooling adoption.
- macOS ARM64 and Linux ARM64 fresh exact-wheel installations with synthetic injected indexing/search/reranking, identity/origin/no-op/refusal and installed-byte checks: OK. These are real prior qualified executions, not a replay attributed to recovery.
- Hash-bound synthetic upgrade from the anonymously downloaded historical wheel: OK; no real-provider or arbitrary-store compatibility guarantee.
- Fresh frozen assay: 22/22 observed. Retained local tooling/control-plane proofs: 10 passed, no warnings.
- Managed recovery/integrity gate: OK; both genuine predecessors, original24, final plan-2/code-2, exact phase budget and artifact inputs.
- Managed ./bin/check all: OK, 268 tests and all lint/format/type/CLI/policy checks.
- Diagnostic review: two recovery-gate and nine full-gate uv environment notices explained by the pinned script environment; canonical wrappers ignore it and select the locked project environment. No override, fallback or test/product warning.
- Handoff gate: pending after all tracked bookkeeping and the required sanitized report; no delivery before it.

Review lane: full. Evidence lane: full. Follow-up route: full-cycle same-phase primary continuation.

Role model/venue — orchestrated by Codex:
- Primary: inline planning/coding under the frozen explicit-configuration receipt; no provider-reported primary identity is inferred.
- Plan reviewer and code critic: original independent Claude Opus/high reports retained with original run/report/span/candidate/native provenance; only final plan-2/code-2 carried. No new invocation, no historical code1 promotion and no ingested plan1 was manufactured.
- Source correction: separately authorized independent Opus/high source review and qualified tests retained privately; recipient acceptance is this primary's own assessment and actual local proofs, not an imported donor verdict.
- Historical native model/version/effort observations remain with their original artifacts; no new harness observation exists in this continuation.

Candidate-bound evidence:
- Product candidate: 4844f3f611961e03d32af7cac9e08887e6ea2f5407fe809e2b335997af0391c6.
- Both managed final commands preserved their respective full-tree/product identities. Final advice joins are unchanged; phase budget remains exactly two PLAN and two CODE with the original four byte-identical rows.
- Normal acceptance validation: EVIDENCE VALID before and after finalization; fresh primary dispositions recorded.
- Supported actual attest/carry/export succeeded. Disposable refusal witnesses rejected a changed budget-prefix digest and a missing real critic predecessor; restored export succeeded. All 24 named original files and old pinned source bytes remain unchanged.
- Finalized timing summaries are valid; no new role intelligence or user wait is fabricated from carried historical advice.

### Execution timing

Execution trace: 79e3b6a6252b4419a9e0a831e2013ddf
- Outcome: `success`
- Makespan: 647285857334 ns (647.285857334 s)
- Intelligence: 0 ns (0.000000000 s)
- Gates: 162677777250 ns (162.677777250 s)
- Orchestration: 646961085208 ns (646.961085208 s)
- Wait: 0 ns (0.000000000 s)
- Retry/rework: 0 ns (0.000000000 s)
- Failed work: 0 ns (0.000000000 s)
- Unattributed: 324772126 ns (0.324772126 s)
- Category totals are interval unions and may overlap.

#### Slowest material spans

- `orchestration.acceptance` — category `reconciliation`, attempt 1, outcome `success`: 331721668750 ns (331.721668750 s)
- `orchestration.setup` — category `reconciliation`, attempt 1, outcome `success`: 313537014166 ns (313.537014166 s)
- `gate.full` — category `gate`, attempt 1, outcome `success`: 161953545375 ns (161.953545375 s)
- `orchestration.implementation` — category `reconciliation`, attempt 1, outcome `success`: 1635322750 ns (1.635322750 s)
- `gate.recovery-integrity` — category `gate`, attempt 1, outcome `success`: 724231875 ns (0.724231875 s)

Acceptance:
- Objective source preparation: final version metadata and described caller behavior; independent advice and fresh primary dispositions; exact locally inspected reproducible packages; two-platform clean installation, bounded synthetic upgrade, supported immutable evidence recovery and managed complete source gates.
- Still unproved objective release criteria: hosted CI, new tested-commit tag/release, uploaded asset hashes and anonymous downloaded-new-artifact installation. These are subsequent authorized release steps, not manual acceptance claims or completed work.
- Parked human judgments: existing quickstart/product/readability/provider walkthroughs remain separate. No new operator decision or publication approval is requested.

Delivery: existing guard after a green final handoff gate, then the already-approved new-version tag/release substeps. Historical v0.1.0 tag/assets/history must remain intact; no package registry, live store, inference, permission/security or service operation is included.

Ripple: none — same existing release, no downstream sketches selected. Existing quickstarts/attribution were not rewritten; the current frozen assay was actually rerun.

Lessons:
- magenta-rottweiler filed: distinguish local attempts from phase-wide allowance; supported two-predecessor attestation now resolves the preserved failure without rewriting it. No duplicate occurrence or rule graduation.
- Existing fixture inventory discipline covers the missed escaped version string; it was corrected and focused/full verified.
- Existing esoteric-hawk graduation recommendation remains outside this release scope; no new policy DECIDE blocks this approved work.

User demo: phase quickstart/synthetic package/inference-boundary review remains available for the operator. Current packaging checks are automated disposable fixtures, not a fabricated interactive demo. Existing readability action remains open and is not a release prerequisite.

Remaining: required report and bare handoff gate; guarded source delivery; exact branch/tag CI; clean delivered-commit artifact comparison; new GitHub release/assets/checksums; anonymous downloaded-new-artifact verification. No publication or phase completion is claimed prematurely.

## 2026-10-21 11:13 — PARK
Public release preparation — mandatory named-phase report hold

Execution trace: 79e3b6a6252b4419a9e0a831e2013ddf

The accounting recovery is complete and the current source-preparation run remains independently advised, primary-dispositioned, managed-gate proved, finalized and accepted. Its accepted closure is immutable; this append-only record does not change that outcome. All 24 original evidence files and exact two-PLAN/two-CODE bytes remain unchanged. Ten focused recipient proofs and the managed full 268-test/all-policy gate passed. Existing qualified packages and platform/upgrade evidence have unchanged inputs.

Actual remaining refusal: the required execution-telemetry dashboard command for phase `release` exits 2 with `accepted trace is absent or belongs to another phase`. The successful finalized root is `phase.release`; lib/agentic_starter/execution_dashboard.py `_phase_for` admits only the numeric PHASE_RE. This repeats the preserved named-identity projection defect under a genuine whole-release commission. No new report root, synthetic phase, omission, checker change or user publication question is introduced.

The release ledger stays in progress. The mandatory report must be supported before the bare handoff gate or guarded source delivery; branch/tag CI, pristine commit build comparison, publication and anonymous downloaded-new-artifact verification remain pending. All commands are terminal. The coordinator receives the concrete current report defect for routing to the retained tooling owner under existing release authority; no second tooling writer is started.

Lessons:
- Added the distinct current occurrence to caped-bonobo, now three observations; this remains a candidate, not automatic rule graduation or a new release approval prerequisite.
- magenta-rottweiler is now resolved by supported real-predecessor attestation while its original failure remains preserved.
- Existing esoteric-hawk remains a separate human graduation recommendation; no standing policy was changed.

## 2026-10-21 12:01 — END (report recovery)

Public release preparation — named report recovery complete

The mandatory release report renders through the qualified live reader against the unchanged successful accepted trace. Only two live reporting modules changed; closed pinned tools, acceptance, reports and exhausted two-PLAN/two-CODE bytes remain exact. The selective source correction was independently qualified before adoption. Fourteen recipient tests and disposable named/numeric positive, malformed/wrong-phase/case refusal and nonexact wait-union checks passed. Presentation templates and assets are unchanged.

This completes the report hold, not publication. The release ledger stays in progress until hosted commit/tag gates, pristine delivered-source artifact comparison, publication and anonymous downloaded-artifact installation prove the remaining criteria. Qualified package inputs remain identical. The final report write and bare full handoff gate precede guarded delivery. Manual readability and provider-quality judgments remain separate.

Lessons:
- caped-bonobo preserves the named-identity projection mismatch and bounded correction, without phase invention or evidence rewriting.
- Private fixture argument and data-envelope parsing mistakes were corrected against the actual handoff schema and canonical parser; rejected diagnostic outputs remain in the execution transcript. No product edits or new duplicate lesson were needed.
- Existing graduation recommendations remain human judgments, not a release prerequisite.

## 2026-10-21 13:35 — END

Palace 0.2.0 — full authorized release published and verified

Palace 0.2.0 is published with a new annotated tag bound to the tested release commit. Historical 0.1.0 tag object, commit and asset bytes remain unchanged. The complete source passed the managed implementation gate, final bare handoff gate and normal tag-push guard. Hosted branch and tag CI each passed full checks, package builds and installed synthetic smoke on macOS and Linux. Pristine exports rebuilt twice with byte-identical qualified archives.

Anonymous public HTTPS GETs retrieved all three release assets without account authentication, cookies or proxy credentials. Both package hashes match the previously qualified archives and published SHA256SUMS. Fresh outside-checkout macOS ARM64 and Linux ARM64 environments installed the downloaded wheel with hash-locked dependencies. Dependency checks, exact installed member comparisons, synchronous indexing/retrieval, recorded identity, unchanged no-op, origin preservation, reranking and mismatched-identity refusal passed with injected fake providers and forbidden inference network connections. No production data, paid inference, deployment, production-service restart, grant change or package-registry publication occurred.

Release: https://github.com/wolfmcnally/palace/releases/tag/v0.2.0

Branch CI: https://github.com/wolfmcnally/palace/actions/runs/39649508975

Tag CI: https://github.com/wolfmcnally/palace/actions/runs/39650844078

The release ledger is completed; no next phase is selected. Original accepted source evidence, rejected attempts, final advice, phase identity and exhausted review budgets remain preserved. This close records remote qualification separately; the original source-preparation timing does not measure the later CI or publication interval. Final factual bookkeeping and its regenerated report receive a fresh bare handoff gate before guarded delivery. No tagged source or published asset is relabeled by that bookkeeping commit.

Build warnings:
- Local source gates are warning-free. Hosted setup reports an already-current jq, Git initialization hints and a pinned checkout-action Node runtime deprecation; both platform jobs completed successfully. Runner label migration notices do not establish the architecture of these exact archives.
- Linux dependency installation may report its known hardlink-to-copy fallback; copies and exact installed-byte checks pass. Platform claims here remain ARM64 for the exact downloaded-wheel environments.

Acceptance:
- Objective release, artifacts, tag/commit, CI, public downloads, installation and synthetic behavior are verified.
- Manual quickstart/readability and real-provider quality judgments remain separate. User Demo: Review the quickstart, synthetic package smoke and documented inference boundaries.

Lessons:
- caped-bonobo preserves the named-report-reader mismatch and its independently qualified correction; originals and accepted trace were not rewritten.
- magenta-rottweiler preserves the local-attempt accounting failure and real-predecessor recovery without extra allowance.
- Existing lesson graduation recommendations and readability review remain operator judgments, not release blockers. No new policy is graduated.
- Recalibration: no target has the required thirty valid successful samples. The advisory reader explicitly skips incomplete historical schema-3 rows; full diagnostics are preserved privately. No timeout settings or old telemetry bytes were changed.
