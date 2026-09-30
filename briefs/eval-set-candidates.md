---
title: "Synthetic evaluation candidate design"
date: 2026-09-30
status: draft
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Synthetic evaluation candidate design

## Public evaluation boundary

Committed questions and expected answers must derive only from synthetic fixtures or a redistributable, pinned corpus. Do not mine an operator's vault, session transcripts, contacts, business records or other private source material for committed questions, source paths or reports. Aggregate-only reporting still requires review for disclosure.

## Candidate coverage

Build discriminating questions for exact lookup, paraphrase, ordinal collisions, multi-hop relationships, temporal changes, contradiction detection and abstention. Include duplicate boilerplate, ambiguous terms and missing answers. Record expected source identities, provenance, tags and the frozen corpus version. Separate authored and generated fixture content.

A candidate is not a validated evaluation result. Verify its expected answer against the fixture and record which retrieval configurations distinguish it. Compare lexical, vector, fused and reranked results with identical input identity and report per-arm contribution.

## Pending relevance validation

Public regression scores do not establish relevance for an operator's real workload. That validation remains pending and must use consented, local-only evaluation with no private questions, answers or source paths committed. Do not tune retrieval defaults based solely on synthetic success. See [fixture design](eval-fixture-corpus.md) and [memory architecture](sota-memory-and-recall.md).
