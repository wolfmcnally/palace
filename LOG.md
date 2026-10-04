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
