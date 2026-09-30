# Fact schema

Palace promotes a small fraction of its episodic observations into durable semantic facts. Each fact is a **natural-language claim** stored as a Markdown section with YAML frontmatter, carrying the claim text, bitemporal annotations, a confidence score, and at least one provenance reference to the originating event(s). This file specifies the schema. The format is binding: every consolidator, CLI, and MCP tool that reads or writes facts conforms to it.

The strategic rationale is in `briefs/sota-memory-and-recall.md` §C.4 (bitemporality) and the substrate-alignment note below. Read those before changing this schema.

## Why prose, not triples

The substrate is prose-first: Markdown claims can be indexed and retrieved without a fixed predicate vocabulary. External consumers must recover each canonical field without a particular editor or private integration.

Concretely, prose-with-frontmatter — and each of these reasons holds for a consumer that has never run Obsidian:

1. **Embeds cleanly** for hybrid retrieval — the claim itself is the chunk; no extraction pass needed before something becomes searchable.
2. **Survives schema drift** — a free-form claim doesn't have to fit a predicate vocabulary that the consolidator might have to evolve later.
3. **Stays hand-editable** in any text editor.
4. **Affords Obsidian where present** (a non-load-bearing bonus, per `policies/surface-vs-substrate.md`) — on a surface that uses Obsidian, wikilinks render as relationships, hierarchical tags as categories, and the body is just text, with no parallel structure to maintain. A headless consumer ignores the rendering and reads the same fields as plain strings.

When a future graph-query workload genuinely demands typed relations (`briefs/sota-memory-and-recall.md` §C.3 describes the evaluation boundary), the consolidator can derive a typed graph *from* the prose facts. The prose remains the source of truth.

## Logical shape of a fact

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | SHA-256 hex of the fact's canonical JSON form. Computed over `{event_time, ingest_time, confidence, provenance, tags, refs, claim}` — `valid`, `superseded_by`, and `id` itself are excluded so they can be patched in place without breaking the id. |
| *(body)* `claim` | string (Markdown prose) | yes | The fact itself, written as one tight paragraph an agent or human can read directly. Lives in the section body, not the frontmatter. |
| `event_time` | ISO-8601 (with offset), or an object `{start, end}` for intervals | yes | When the claim was true in the world. End may be `null` for open-ended. |
| `ingest_time` | ISO-8601 with offset | yes | When palace recorded the fact. Set by the consolidator, not the source. |
| `confidence` | float in [0, 1] | yes | The consolidator's confidence at promotion time. Decay rules will live in a future `policies/consolidation.md`. |
| `provenance` | list of source-event ids (SHA-256 hex) | yes; ≥ 1 | Every fact has at least one source. Hooks-captured events qualify; raw conjecture does not. |
| `valid` | boolean | yes | Defaults `true`. Set `false` on invalidation. Patched in place. |
| `superseded_by` | id of replacement fact | optional | When a later fact replaces this one. Patched in place. |
| `tags` | list of strings | optional | Free-form category strings (`sample-project`, `decisions`, `infrastructure`). Hierarchical `a/b` is allowed and renders as nested tags in Obsidian; a headless consumer reads them as plain strings. |
| `refs` | list of strings | optional | References to related notes/entities, serialized as `[[target]]` wikilinks (e.g. `[[sample-project/architecture]]`). The relationship layer, in lieu of typed predicates. Wikilink syntax is the on-disk rendering of a neutral target identifier (Obsidian and other wiki tools follow it as a link; a headless consumer reads the target string directly). |

## On-disk encoding

Facts live in `<vault-root>/facts/MEMORY.md` — the CLI convenience `<vault-root>` defaults to `~/Obsidian/Palace/`, but it is the parametric vault root of `policies/storage-layout.md` rule 9, so a consumer points it at its own location. They are stored as a sequence of Markdown sections, one per fact, in chronological-by-ingest order. Each section opens with an H2 heading (a short human-readable summary the consolidator generates from the claim), then a YAML frontmatter block delimited by `---`, then the claim body.

```markdown
## a publishing workflow adopted sqlite-vec after evaluating Qdrant and pgvector

---
id: 7f1e3a9b1c4d...
event_time:
  start: 2026-04-01
  end: null
ingest_time: 2026-05-17T14:33:00-06:00
confidence: 0.82
valid: true
provenance:
  - event:b7e3a1c4...
  - event:5c01ff9d...
tags: [example, infrastructure, decisions]
refs:
  - "[[sample-project/architecture]]"
  - "[[sqlite-vec]]"
---

a publishing workflow adopted sqlite-vec on 2026-04-01 after evaluating Qdrant and pgvector.
The decision was driven by single-process embedding and the small-vector-count
workload of book content. Redis was rejected as not vector-capable.
```

The body — the claim itself — is what gets embedded for vector search and tokenized for BM25. The heading is for human scanning and Obsidian's TOC; it is not part of the id. The frontmatter is the structured side: filters, gates, provenance walks. JSON projection (for MCP returns, indexes, or backup streams) is straightforward — the YAML frontmatter is JSON-equivalent and the body becomes a `claim` string field.

## Lifecycle

1. **Candidate.** The consolidator scores an episodic signal and considers promoting it.
2. **Promoted.** Candidate passes gates → written as a new section in `MEMORY.md`.
3. **Invalidated.** A later observation contradicts the fact and the consolidator (or the operator) patches the section's frontmatter to set `valid: false`. The patch is recorded as a new event in `events/` (a `fact-invalidate` event whose payload references the fact's `id`). The body and the original frontmatter fields are not edited.
4. **Superseded.** A new fact is written as a fresh section with a fresh `id`. The old section's frontmatter is patched to set `superseded_by: <new-id>` (and typically `valid: false`). Same event-recording rule as invalidation.

`valid` and `superseded_by` are the explicit mutation surface; everything else in the frontmatter and the body is immutable once written. Editing the body, the claim, or any of the immutable fields requires writing a new fact and superseding the old — never an in-place rewrite, because that breaks the id and the provenance chain.

## Gates for promotion

The consolidator promotes a candidate only when **all** of:

- `relevance > 0.5` *(query-relevance proxy: how often this signal would have helped recent queries)*
- `confidence > 0.6` *(LLM-extractor confidence after evidence integration)*
- `corroboration ≥ 2` *(at least two independent source events, OR one explicit operator-authored source)*

Failures route to `dreams/DREAMS.md` with the score breakdown and the candidate claim, so the operator or a later review can promote manually. Default thresholds borrow from OpenClaw's Deep Sleep promotion weights; they are tunable, but changes belong in a successor `policies/consolidation.md` once the consolidator exists.

## Provenance and ids

Every event written to `events/`, `sessions/`, or `raw/<source>/` carries an `id` field that is the SHA-256 hex of its canonical JSON form (sorted keys, no whitespace, UTF-8). Fact ids are computed the same way over the canonical JSON form of `{event_time, ingest_time, confidence, provenance, tags, refs, claim}`.

A consumer that wants to verify a fact's provenance walks the `provenance` list, fetches each referenced event by `id`, recomputes its hash, and confirms the integrity of the chain — all with `sha256sum` and `jq`. No special tooling required.

## Tags, refs, and the deliberate absence of a predicate vocabulary

Palace **deliberately does not impose a predicate vocabulary**. The substrate is prose; categorization happens through `tags` (plain category strings), and association happens through `refs` (target identifiers serialized as `[[wikilink]]`s). These are substrate-neutral primitives with a deliberate Obsidian affordance: the wikilink and hierarchical-tag *serializations* interoperate with existing editors, so palace borrows the on-disk syntax rather than inventing a parallel typing system — but the *meaning* (a category, a pointer to another note) is recoverable without Obsidian, per `policies/surface-vs-substrate.md`.

If a future workload demands typed relations (graph traversal, multi-hop questions that hybrid retrieval handles poorly), the consolidator can derive a typed graph as a *projection* of the prose facts — for example, by running an LLM extractor over claims to produce `(node, edge, node)` triples on demand, materialized into `index/graph.db`. The prose stays canonical; the typed graph is derived and rebuildable.

## Out of scope for this policy

- **Entity resolution / registry** — canonicalizing the names that appear in claims (for example, associating an abbreviation and full name). Defer until palace has enough facts that this hurts.
- **Cross-machine sync** — palace is single-machine for now. When this changes, the sync protocol gets its own policy.
- **Predicate vocabulary** — deliberately none. See above. If palace ever needs one, it will be a derived projection, not a constraint on the canonical store.

These will be added as work demands them, not in advance.
