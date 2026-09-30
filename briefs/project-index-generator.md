---
title: "Project index generation"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Project index generation

## Design

A consumer supplies a source tree and an explicitly selected store. `palace index build` reconciles the index synchronously and exits; incremental operation and synchronous builds share indexing semantics. The consumer owns corpus selection, completeness, deployment and artifact publication.

## Portability

Index paths are relative to their watch root so a copied SQLite artifact does not embed a developer's home layout. Embedding identity and convention remain recorded and validated. Copies of a database require a consistent snapshot; an ordinary file copy of an active writer is not a publication protocol.

## Boundaries

The default personal store remains local. A publishing workflow uses a separate store and explicit provider authorization. Daemon support is a distinct operational surface; a synchronous build does not establish cross-platform daemon qualification. Source data remains authoritative and indexes rebuildable. Legacy absolute-path derived indexes require an explicit rebuild, not silent mixed-format interpretation.
