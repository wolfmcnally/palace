---
title: "Public evaluation fixture design"
date: 2026-09-30
status: draft
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Public evaluation fixture design

## Frozen, redistributable fixtures

A fixed corpus is necessary to attribute a retrieval regression to a configuration change. Use synthetic text or material with documented redistribution rights, retained source metadata and stable checksums. Rights and provenance for the included fixture are recorded with its manifest and public evaluation documentation; do not infer redistribution permission merely from availability on an archive site.

## Required hazards

Include duplicate boilerplate, colliding ordinal identifiers, ambiguous terms, recurring entities, temporal changes, OCR noise, paraphrase and genuinely absent answers. Pin both corpus and question identities. Reports must distinguish inspected facts, generated expectations and unverified candidates.

## Relevance gap

Fixture scores qualify regression behavior on the fixture. They do not establish relevance for private operator workloads. That separate validation remains pending and must never introduce private source paths, question text or expected answers into public artifacts. See [candidate design](eval-set-candidates.md).
