---
title: "Embedding providers and recorded index identity"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Embedding providers and recorded index identity

## Identity contract

An index records model, provider, dimension and embedding convention. Builders and readers validate these axes before writes or query inference; equal dimensions do not make vectors interchangeable. Never silently substitute another upstream provider or combine incompatible spaces.

## Authorization

Local Ollama is the absent-config default. Third-party inference requires an explicitly opted-in already-published corpus. Operator-controlled private HTTPS inference is a separate attested boundary using the native adapter and credential references. The default personal store and vault overlaps remain unconditionally refused for remote inference.

## Reproducibility

Remote endpoints may be nondeterministic; provider/model labels alone do not prove byte determinism. Pin available identity metadata, record attempted-request audits, and qualify retrieval rather than assuming identical vectors. Configuration alone must make no inference request.

## Throughput

Remote concurrency is bounded and explicit. Preserve local single-caller behavior. Qualified timing and cost limits must name the actual endpoint, input regime and environment; this public brief contains no private throughput run evidence.
