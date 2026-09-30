---
title: "Contextual enrichment of embedded chunk text"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Contextual enrichment of embedded chunk text

## Scoring text

Use deterministic file/path and heading breadcrumbs when constructing embedding and reranker input. Preserve stored chunk content and neutral relative identity; scoring context is derived rather than a second canonical text source.

## Silent-staleness hazard

An embedding convention change changes vector meaning even if model and dimension are unchanged. Record and verify convention identity. Rebuild incompatible derived embeddings explicitly; do not continue a mixed-convention index merely because vector lengths agree.

## Evaluation

Measure the effect with fixed corpus, questions and provider identity. Distinguish regression results from representative workload relevance; no private corpus measurements are included here.
