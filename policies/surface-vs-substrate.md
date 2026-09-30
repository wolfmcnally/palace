# Surface vs. substrate

Palace is a **substrate**, not a single-device store. Consumers may use shared or isolated indexes, local agents or headless processes. Canonical data must remain expressible without a particular consumer application.

Obsidian is an optional viewer/editor for Markdown facts. The documented vault default is a convenience; no private deployment or user's editing habits are part of the data model.

## The rule: afford the surface, don't depend on it

**No device-specific or viewer-specific surface may be load-bearing in palace's canonical layer.** The canonical layer is exactly three things:

1. The **bitemporal fact model** (`policies/fact-schema.md`) — `event_time`, `ingest_time`, `confidence`, `provenance`, `valid`/`superseded_by`, `claim`, SHA-256 `id`.
2. The **Markdown-with-frontmatter encoding** plus the append-only JSONL streams (`policies/storage-layout.md`).
3. A **parametric `<store>` and `<vault-root>`** (`policies/storage-layout.md` rule 9) — never a hardcoded absolute path.

Palace **affords** interoperable surfaces — it serializes refs as `[[wikilink]]`s, allows hierarchical `a/b` tags, and keeps frontmatter Dataview-readable, all of which Obsidian renders natively. Those are *serializations and conveniences*, not *dependencies*. The test is binding:

> **A consumer with no Obsidian (a headless process or a local agent) must be able to read, write, and follow every canonical field with `sha256sum`, `jq`, and a Markdown parser — nothing more.**

If a field's *meaning* can only be recovered by a specific viewer, it is mis-specified. Wikilink syntax is a rendering of a neutral pointer (a target note/entity identifier); hierarchical tags are a rendering of neutral category strings; the file's location is a parameter, not the path `~/Obsidian/Palace/`.

## What this constrains

- **Field definitions** name substrate-neutral semantics first and the Obsidian serialization second. "References to related notes, serialized as `[[target]]` wikilinks" — not "Obsidian wikilink strings." "Category strings; hierarchical `a/b` allowed" — not "follows Obsidian conventions."
- **Justifications** for design choices stand without Obsidian. "Interoperates with Obsidian" is a closing affordance, never a load-bearing reason; the load-bearing reasons must hold for a consumer that has never run Obsidian.
- **Locations** are presented as `<store>` / `<vault-root>` with `~/palace-data.noindex/` and `~/Obsidian/Palace/` as **documented CLI defaults**, per storage-layout rule 9 and `briefs/store-parametric-external-consumers.md`. Policy and brief prose must not narrate the defaults as though they are the only paths.
- **New surfaces** (a future web viewer, a different vault tool, a sync target) are added as consumers of the canonical layer, never by extending the canonical layer to require them.

## Why this is a policy, not a preference

Palace's whole thesis is "one substrate, many agents." A canonical field whose meaning lives in one editor on one machine quietly re-couples the substrate to a single surface and breaks every other consumer — the exact silo `briefs/store-parametric-external-consumers.md` and the "One substrate, many agents" invariant exist to prevent. Affording Obsidian costs nothing and supports rich local editing; depending on it forfeits the substrate. `plan-reviewer` and `code-critic` block on a canonical field, justification, or location that cannot survive the no-Obsidian test above.

Motivating brief: `briefs/store-parametric-external-consumers.md` (the same decoupling, one layer down, for the Phase 5/6 CLIs).
