---
title: "Store-parametric fact and consolidation engine"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Store-parametric fact and consolidation engine

## Independent roots

The machine-state `<store>`, human-readable `<vault-root>` and derived index are explicit locations. Existing CLI defaults remain convenience defaults, not the only permitted locations. A consumer must be able to select isolated roots without reading or writing another operator's data.

## Contract

Fact operations preserve the generic Markdown/frontmatter schema and provenance. Consolidation reads its declared source adapter, promotes qualifying facts and records held contradictions for operator adjudication. The selected input format, trust mapping and empty-source handling are explicit; changing paths alone cannot make an unsupported source format readable.

Consumers own source selection, access, backups and reference updates. Palace does not infer consumer policy from neighboring repositories. See [source adapters](consolidator-extraction-source-pluggability.md).
