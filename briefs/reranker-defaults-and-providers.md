---
title: "Reranker defaults and providers"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Reranker defaults and providers

## Shipped behavior

The CLI enables reranking by default; `--no-rerank` selects fused retrieval. Library entry points expose strict failure by default. Local model artifacts are shared runtime state, acquired and verified transparently; loading and digest verification remain visible in end-to-end latency.

## Failure and provider boundaries

Distinguish applied, disabled and failed reranking. An explicitly lenient boundary may return fused results with failure status; authentication, identity, schema and audit failures do not degrade. Private endpoints use the explicit authorization contract and cannot substitute a provider. A planned provider is not a shipped integration merely because this design mentions it.

## Evaluation debt

Representative relevance evaluation remains pending. The shipped default is an explicit exception to the evaluation-before-tuning rule and must be confirmed or reversed by that evaluation. Synthetic regression scores do not discharge it. Pool depth, token budget, model or expansion changes require relevant evaluation first.
