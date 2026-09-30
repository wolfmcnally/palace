---
title: "Structured external indexing"
date: 2026-09-30
status: draft
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Structured external indexing

## Proposal, not shipped promise

Extend the generic indexing core to accept stable structured records, typed metadata, multiple retrieval representations and independent vector spaces. This remains a design proposal; support is established only by current API documentation and tests, not by this brief.

## Producer and consumer responsibilities

A consumer supplies authorized records with stable source identity and owns domain semantics, completeness and publication. Palace reconciles the supplied surface and reports failures. It does not infer a corpus manifest, domain registry, client workflow or deployment intent.

## Identity and reconciliation

Representations of one source unit retain their relationship without collapsing different vector spaces. Each space has explicit model/provider/dimension/convention identity. Filters and lexical profiles need typed validation. Incremental replacement and deletion need precise scope so a partial batch cannot erase unrelated records.

## Acceptance before implementation

Define hermetic synthetic cases for multiple representations, incompatible spaces, interrupted reconciliation, removed records and absent input. Resolve persistence, migration, query fusion and cost visibility in a concrete plan before making public support claims.
