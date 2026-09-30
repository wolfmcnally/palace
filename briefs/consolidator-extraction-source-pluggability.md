---
title: "Pluggable consolidation extraction sources"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Pluggable consolidation extraction sources

## Source adapters

Session transcripts and captures have different partitioning and trust semantics. Source selection must be explicit. Session records resolve their transcript pointers; captures use their declared directory, range and source-kind adapter. Do not silently treat an unread source format as an empty successful run.

## Trust and promotion

`SourceUnit.authored` is the generic single-authority signal. Interactive top-level session turns and explicitly trusted capture prefixes have different predicates. Preserve distinct-event corroboration counts independently from frequency weights. The legacy helper name `is_wolf_authored` is an implementation identifier, not a rule restricted to one person.

## Empty and historical input

Requested date/range processing must visibly report empty input and preserve idempotent per-source progress. Backlog processing must not masquerade as replay of the current day. Consumers supply source access and approval; the generic engine does not assume a private application's file layout.
