---
slug: small-crane
title: Type intentional invalid-input fixtures and include tests in focused typechecking
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-09
source: kickoff
occurrences:
  - date: 2026-11-09
    ref: "Phase 24 snapshot-budget correction full typecheck"
---

As of 2026-11-09, runtime-focused mypy passed and all behavioral tests passed, but the complete gate rejected two negative-test calls. The test deliberately supplied noninteger keyword values to integer-only parameters; inference correctly rejected those calls. The focused typecheck had named only runtime files and therefore never examined the changed fixture.

Make intentional static-contract violations explicit at the narrow fixture boundary, retaining every runtime value and refusal assertion. Include changed test files in focused typechecking, or use the repository's complete typecheck when their imports require its configured context. A behavioral pass cannot establish static correctness, and an unchecked test surface must not inherit the runtime check's result.
