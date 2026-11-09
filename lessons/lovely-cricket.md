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
---

As of 2026-11-09, a schema-shaped independent advisory report passed source generation but failed ingestion because affected_paths contained an absolute scratch-plan path. The generated JSON schema only required strings; the semantic validator required repository-relative paths. The original report and frozen candidate were preserved. One policy-authorized artifact-only re-emission removed the non-repository path without changing the findings; the later code-advice prompt stated the path constraint explicitly and ingested successfully.

Before an advisory launch, provide semantic constraints enforced beyond the output schema, including the admissible path namespace. A narrow shared generation/ingestion contract could prevent this mismatch; that is a proposed methodology improvement, not a change authorized by this product phase. Never repair an independent report by editing its output or claim its substantive review was unsuccessful merely because its artifact encoding was rejected.
