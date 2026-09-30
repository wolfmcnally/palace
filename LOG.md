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
