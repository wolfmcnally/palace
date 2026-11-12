---
slug: lovely-cricket
title: Give advisory reviewers semantic artifact constraints before launch
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-09
source: kickoff
occurrences:
  - date: 2026-11-09
    ref: "Phase 24 plan advice artifact re-emission"
  - date: 2026-11-11
    ref: "Phase 27 plan review attempt 1 refused at ingest: two findings cited the out-of-tree plan artifact"
  - date: 2026-11-11
    ref: "Phase 29 plan review attempt 1 refused at ingest: one finding cited docs/../daemons/reindex/README.md although the prompt forbade '..'"
---

As of 2026-11-09, a schema-shaped independent advisory report passed source generation but failed ingestion because affected_paths contained an absolute scratch-plan path. The generated JSON schema only required strings; the semantic validator required repository-relative paths. The original report and frozen candidate were preserved. One policy-authorized artifact-only re-emission removed the non-repository path without changing the findings; the later code-advice prompt stated the path constraint explicitly and ingested successfully.

Before an advisory launch, provide semantic constraints enforced beyond the output schema, including the admissible path namespace. A narrow shared generation/ingestion contract could prevent this mismatch; that is a proposed methodology improvement, not a change authorized by this product phase. Never repair an independent report by editing its output or claim its substantive review was unsuccessful merely because its artifact encoding was rejected.

As of 2026-11-11, the Phase 27 design plan lived in the evidence run directory, and its first plan-review prompt did not state the repository-relative constraint. Two of twelve findings cited the out-of-tree plan path, and ingestion refused the whole report. The coordinator authorized the phase's second and final pass with a recorded cause; the corrected prompt required repository-relative paths and named plan/phase-27.md for plan-level findings, and that report ingested. This lesson already recorded both the defect and a cheaper artifact-only route, but the ledger was not re-read before launch, so the constraint was missing again and a review pass was spent. Read this lesson before every advisory launch whose artifact lives outside the tree, and state the path constraint in the prompt.

As of 2026-11-11 the prompt for Phase 29's plan review stated the repository-relative rule, including no '..', and one of ten findings still emitted a path with a '..' component. Stating the rule lowers the rate but does not guarantee conformance. The coordinator authorized the policy's one artifact-only re-emission in the same reviewer session, with the exact executable rule and the rendered live schema embedded and the candidate measured before and after; the re-emitted report matched the original except for that path and ingested. Keep the rule in every advisory prompt, and treat a residual encoding refusal as a re-emission case rather than a lost review pass.
