# Storage layout

Palace stores state in **two roots**: a machine-state **`<store>`** and a human-readable **`<vault-root>`**. The split is deliberate: human-readable Markdown surfaces live in the vault so a viewer (for example, Obsidian) indexes them into its graph, search, and quick-switcher; machine-only JSONL and derived indexes live outside the vault so they do not pollute it.

In the default CLI configuration these roots default to the paths below, and every absolute path in this file is that default. But the roots are **parametric** (rule 9), not hardcoded: an external consumer points `<store>` and `<vault-root>` at its own locations and runs palace's engine over an isolated store — Obsidian is one *surface* over the vault, not the substrate. See `policies/surface-vs-substrate.md` and `briefs/store-parametric-external-consumers.md`.

```
~/palace-data.noindex/         # <store>       — machine state, NOT inside any vault
~/Obsidian/Palace/             # <vault-root>  — human-readable Markdown, Obsidian-indexed on this device
```

The third location to keep in mind is `~/palace/`, this Git repo — code, briefs, policies, plan, agent definitions. Three roots, three jobs:

| Root | Contents | Git-tracked? | Obsidian-indexed? |
|---|---|---|---|
| `~/palace/` | repo (code, briefs, policies, plan, agents, skills) | yes | no |
| `~/palace-data.noindex/` | machine state (events, sessions, raw, indexes, locks, logs) | no | no |
| `~/Obsidian/Palace/` | facts, dreams, context — vault-quality Markdown | no | yes |

Putting machinery outside the vault is the part that changed when palace stopped overloading the Obsidian directory: at the volumes palace will reach (one session JSONL per agent turn, one events line per FSEvents change, raw-source dumps per connector tick), the vault would be dominated by files that aren't notes. Obsidian's strengths apply to the Markdown surfaces palace promotes *to* the vault, not the raw streams it collects *for* later promotion.

`~/palace-data.noindex/` is intentionally visible in `~` (lowercase, no leading dot) so the operator can `cd` to it and inspect machine state directly. Discoverability is a feature; hiding it under `~/Library/` or `~/.palace/` would be hiding work palace is doing on the operator's behalf.

The **`.noindex` suffix is load-bearing**, not cosmetic. macOS Spotlight skips any folder whose name ends in `.noindex` (and everything inside it). The suffix is the file-borne equivalent of adding the directory to System Settings → Spotlight Privacy, with the difference that the rule travels with the repo and applies on every machine palace lands on — no GUI step, no per-machine drift. This matters because palace's machine state is high-churn (every captured session, every reindex tick, every nightly consolidator pass writes here) and not useful as Spotlight hits; left at a bare `palace-data/`, Spotlight would re-index after every change and compete with palace's own re-reads. The Obsidian vault stays Spotlight-indexed by default; only `~/palace-data.noindex/` carries the suffix.

`~/Obsidian/Palace/` is intentionally inside the vault so Obsidian indexes the Markdown into its graph, search, and quick-switcher. The directory is named `Palace` (capitalized, no leading dot) so it shows up in Obsidian's file explorer alongside other top-level folders.

## Canonical layout

### Machine state — `~/palace-data.noindex/`

```
~/palace-data.noindex/
  events/YYYY-MM-DD.jsonl          # append-only event log; every observation lands here
  events/cloud-egress/YYYY-MM-DD.jsonl # append-only cloud audit; invisible to the index day-file tail
  sessions/YYYY-MM-DD/<id>.jsonl   # captured Claude Code / Codex session transcripts + tool calls
  raw/<source>/YYYY-MM-DD.jsonl    # external connector dumps (clipper, plaud, calendar, mail, slack…)
  index/                           # derived indexes — sqlite-vec, BM25, eventually graph DB
    chunks.sqlite                  # vector + FTS5
    graph.db                       # later: LadybugDB / Kuzu fork
  meta/
    schema-version                 # one-line version marker for index/ shape
    index-daemon-state.json        # lifecycle plus typed park cause; removed on clean shutdown
    consolidator.lock              # exclusive lock during dreaming
  logs/
    palace-capture.log             # capture daemon stderr, written by launchd via StandardErrorPath
```

Reranker models are deliberately absent from this layout. They are shared
palace runtime state, resolved independently of every store as specified by
`policies/retrieval.md` §2.

The derived daemon-state path is `meta/index-daemon-state.json`; parked records
distinguish an auto-recoverable identity cause from a terminal writer-startup
cause. The append-only cloud audit path is
`events/cloud-egress/YYYY-MM-DD.jsonl`.

### Human-readable Markdown — `~/Obsidian/Palace/`

```
~/Obsidian/Palace/
  facts/MEMORY.md                  # unbounded canonical fact store (Markdown + YAML frontmatter; see policies/fact-schema.md)
  context/
    working.md                     # curated startup-injection view, char-capped (~3,000 chars)
  dreams/DREAMS.md                 # consolidation audit log (kept and discarded candidates)
```

The provenance chain crosses both roots: a fact in `~/Obsidian/Palace/facts/MEMORY.md` carries a `provenance` list of event ids; verifying the chain walks those ids into `~/palace-data.noindex/events/*.jsonl` and `~/palace-data.noindex/sessions/*/*.jsonl`. Palace addresses events by SHA-256 hex id, not by filesystem path, so the split is transparent to provenance verification (`sha256sum` + `jq` still works end-to-end).

## Rules

### 1. Append-only is sacred

Everything in `~/palace-data.noindex/events/` (including
`events/cloud-egress/`), `~/palace-data.noindex/sessions/`, and
`~/palace-data.noindex/raw/<source>/` is **append-only**. No process ever edits,
reorders, or deletes entries from these files. The consolidator reads them;
downstream indexes can be rebuilt from them. They are the source of truth.

If an event needs to be retracted, the *retraction* is itself a new event (`{"type": "retract", "ref": "<event_hash>", "reason": "..."}`) appended after the fact. The original stays.

### 2. Indexes are derivable

Everything under `~/palace-data.noindex/index/` is a derived view over `~/palace-data.noindex/{events,sessions,raw}/` + `~/Obsidian/Palace/facts/MEMORY.md`. Any index file can be deleted at any time and rebuilt by replaying the source files. No data lives *only* in an index.

### 3. Facts are bitemporal and provenance-tracked

`~/Obsidian/Palace/facts/MEMORY.md` holds promoted semantic facts in the format specified in `policies/fact-schema.md`. Facts carry both event time (when the world had that state) and ingest time (when palace observed it). Facts are **invalidated** or **superseded**, never deleted. Each fact lists the ids of the source events it was promoted from, so the provenance chain is plain-text auditable end-to-end.

### 3a. The fact store is unbounded; the startup-injection view is bounded

`~/Obsidian/Palace/facts/MEMORY.md` grows without bound — every promoted fact stays. Sessions reach the body of it via `palace search` rather than by loading the whole file. The file that *does* get loaded at session start is `~/Obsidian/Palace/context/working.md`, a curated view maintained by the consolidator (and editable by the operator) and held to ~3,000 characters. Sizing budget: small enough to inject every session without burning prompt-cache headroom, large enough to carry the active threads, environment notes, and pending decisions the agent needs without a recall step.

The relationship between the two:

- `facts/MEMORY.md` is the **canonical durable store** — long-term, append-mostly, fully-attributed.
- `context/working.md` is the **frozen snapshot loaded at session start** — short, curated, prompt-cache-friendly. Per the CLAUDE.md "in-session writes take effect next session" rule, mid-session edits land on disk but become visible next session.
- The consolidator promotes facts from `facts/MEMORY.md` into `context/working.md` based on recency and relevance, and prunes stale entries to stay under the cap. A `working.md` over its cap is itself a consolidator failure to surface, not an acceptable steady state.

### 4. Daily files are timezone-stable

All `YYYY-MM-DD` files (under `~/palace-data.noindex/events/`, including the
cloud-egress subdirectory, and under `~/palace-data.noindex/sessions/`) use
the currently fixed application timezone (`America/Boise`) consistently across capture and consolidation. This is a current implementation constraint, not an inferred user location. The
within-file timestamps are ISO-8601 with offsets.

### 5. JSONL only — one event per line

Every `.jsonl` file is one JSON object per line, no pretty-printing, UTF-8, LF-terminated. The fact-record schema lives in `policies/fact-schema.md`; per-source event schemas will be defined in per-source briefs as connectors land.

### 6. Hot path vs cold path

- **Hot path** (Stop hooks, FSEvents handlers, clipper drops): write structured raw events to `~/palace-data.noindex/events/`, `~/palace-data.noindex/sessions/`, or `~/palace-data.noindex/raw/<source>/` and return immediately. No LLM calls, no extraction, no enrichment.
- **Cold path** (nightly consolidator, on-demand re-index): read raw, score candidate facts, promote winners to `~/Obsidian/Palace/facts/MEMORY.md`, rebuild indexes under `~/palace-data.noindex/index/`, write `~/Obsidian/Palace/dreams/DREAMS.md` audit entries.

Implementations must respect this split. A capture-time extraction breaks the latency budget; a consolidator that bypasses the audit log breaks accountability.

### 7. Locks are exclusive and short

Only the consolidator takes `~/palace-data.noindex/meta/consolidator.lock`, and only for the duration of a single dreaming pass. Held lock + stale process = the implementation must detect and recover, not block forever.

### 8. Schema migrations are explicit

`~/palace-data.noindex/meta/schema-version` is bumped whenever the on-disk shape under `~/palace-data.noindex/index/` changes incompatibly. Source data under `~/palace-data.noindex/{events,sessions,raw}/` and `~/Obsidian/Palace/facts/MEMORY.md` does not have a version — those are Markdown / JSONL, append-only, and forward-readable.

### 9. The split is enforced at the daemon boundary

Code paths that write to disk take *either* a `--store` argument (the machine-state root, default `~/palace-data.noindex/`) *or* a `--vault-root` argument (the human-readable root, default `~/Obsidian/Palace/`) — never one path with two meanings. A surface that needs both (e.g., the Phase 6 consolidator, which reads `~/palace-data.noindex/sessions/` and writes `~/Obsidian/Palace/facts/MEMORY.md`) takes both flags. No single flag is allowed to be ambiguous about which root it names.

## Backup posture

Operators choose backup scope, destination, retention and access. Palace provides no automatic backup guarantee. Back up the selected source-of-truth roots; derived indexes may be rebuilt. Moving roots or using a sync service changes the operational risk and requires explicit review. Repository Git history is not a backup of runtime stores or uncommitted data.

## Open questions deferred to implementation

- Whether to git-track any part of `~/Obsidian/Palace/` (probably not the indexes; possibly the facts and dreams).
- Whether daily session files should be sharded by repo as well as by date.
- Whether to mirror `~/palace-data.noindex/events/` to a write-once-read-many object store for tamper-evidence beyond local provenance marks.
- Whether the launchd-supervised capture daemon's log belongs under `~/palace-data.noindex/logs/` (current decision) or under macOS's conventional `~/Library/Logs/` (alternative). The current decision keeps all machine state under one root; if Console.app integration becomes a felt need, revisit.

These are tracked in `briefs/sota-memory-and-recall.md` §G and downstream; revisit once the first implementations are running.
