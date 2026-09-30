# Consolidation

The consolidator is palace's episodic-to-semantic promotion engine — the "dreaming" pass that reads a day's captured sessions and events, extracts candidate facts, scores them, gates them, and promotes the winners into `<vault-root>/facts/MEMORY.md`. This policy is palace's first policy carrying **tunable knobs**: the score weights, the shaping parameters, and the model defaults the engine reads. Every knob has exactly one home — `palace/consolidate/config.py` — and this file documents the rationale so a later re-tune is a deliberate, audited change rather than a magic-number edit.

The strategic rationale is in `briefs/sota-memory-and-recall.md` §D.2 (the scoring + thresholds + human-in-the-loop pattern) and §A.2 (dreaming as a production pattern). The on-device-model posture is `policies/local-first.md`. The gate values are owned by `policies/fact-schema.md` §"Gates for promotion" — this file restates, never redefines, them.

## Extraction sources

The consolidator's **extraction source is pluggable in shape**, not just relocatable in path (`briefs/consolidator-extraction-source-pluggability.md`). Two source *kinds* exist, selected by `--source-kind {sessions,captures}` (the kind is also inferred as `captures` when `--captures-root` is supplied):

### Sessions (the default and only the operator-store source)

Durable facts are extracted from **captured agent session transcripts** — the day's `<store>/sessions/<YYYY-MM-DD>/<id>.jsonl` records, each resolved through its `transcript_path` pointer to turn-by-turn dialogue. The session/interaction stream is the *only* fact-extraction source for the operator's store, and is what `palace consolidate` reads absent any captures flag. The reader is `palace/consolidate/sources.py::read_day_sources`.

**Filesystem-change (`fs_change`) events are explicitly NOT a fact source.** The `<store>/events/<YYYY-MM-DD>.jsonl` stream — a record per file the watcher saw change — is a **re-index trigger** consumed by the index daemon (Phase 2/4) to keep `chunks.sqlite` fresh; it is never mined for durable facts.

The rationale is twofold:

- **2026 BCP consensus.** Every production memory system surveyed treats file changes as a re-index signal and extracts durable facts from the conversation/interaction stream alone: OpenClaw re-indexes on file change but its Dreaming pass extracts from sessions; Zep/Graphiti, Mem0, and Microsoft Foundry extract from the interaction stream only; Cognee treats ingestion-source changes as a re-embed trigger, not a fact event.
- **Palace's own A/B finding.** Feeding `fs_change` records into the extractor produced only near-duplicate noise (~50 redundant "Obsidian Vault Structure" candidates in one run) that failed every promotion gate while costing LLM calls. A file changing says *something happened*; it does not say *what was decided or true* — that signal lives in the session that drove the change.

### Captures (the external-consumer source kind)

For a consumer whose episodic ground truth is **a flat directory of pre-distilled Markdown capture files** rather than day-partitioned session transcripts, the captures source kind reads each file under `--captures-root <dir>` into the same `SourceUnit` the extractor consumes. A capture file is named `<slug>-<12hex>.md` and is YAML frontmatter plus a verbatim prose body:

```markdown
---
id: <64-hex SHA-256, content-addressed>
event_time: 2026-06-15T12:00:00+00:00
ingest_time: 2026-06-15T12:00:00+00:00
source: "journal:<entry-id>"
tags: [foo, bar]
---

The durable learning, written as prose — this is the claim body.
```

The mapping is direct and lossless: `source_id` ← frontmatter `id` (already a palace-canonical SHA-256 hex, used verbatim for provenance + corroboration), `text` ← the Markdown **body, read verbatim** (no `transcript_path` resolution and no char-budget windowing — captures are already distilled), `event_time` / `ingest_time` ← frontmatter. The reader is `palace/consolidate/captures.py::read_captures_dir`. A file whose name does not match the capture shape is **skipped** (it is some other Markdown file); a file that matches but is genuinely malformed (bad frontmatter, missing/non-64-hex `id`) raises loudly with the path. Scoring, gating, contradiction, promotion, audit, and lock stages are **unchanged** between the two kinds — only the reader differs.

The consolidator still **writes** its own run record to the events stream (`append_consolidate_event`) as an audit append — that is an append, not a read-as-source, and is unaffected by these rules.

## Backlog and the consolidation watermark

The sessions kind consolidates one calendar **day** (`--date`, default today). The captures kind has no natural day partition: it drains the **unconsolidated backlog** — by default the whole inbox (every capture not already in the watermark), narrowed optionally by `--since <YYYY-MM-DD>` (only captures whose `event_time` is on or after the date). There is no `--all` flag; whole-inbox is the default.

To avoid re-extracting the entire history every run (wasteful of LLM calls even though the novelty gate already suppresses re-*promotion*), the captures kind keeps a **consolidation watermark**: an id-set of the captures already consolidated, written to `consolidate-watermark.json`. The watermark is **rebuildable machine-local state**, not portable ground truth, so it lives under the derived-index / lock location — directory precedence: `--index-db` parent, else `--lock-path` parent, else `<store>/meta/` — and a consumer gitignores it. A **dry-run leaves the watermark untouched**. After a real run the watermark advances over every unit *read and extracted* this run (not only the promoted ones), so a re-run over the same directory drains to a clean zero-candidate result rather than re-extracting.

A captures run whose watermark drains all units (post-filter empty) is a **clean zero-promoted result**, not an empty-source error — see the empty-source guard below.

## Empty-source guard

The consolidator distinguishes "consolidated, nothing met the gates" from "found no source units at all." A selected episodic source that resolves to **zero reader units** (an empty sessions day, or a captures directory with no matching files) is a **loud, non-zero-exit** condition (`EmptySourceError`): a `palace consolidate` run exits 1 with an `error:` line, and in `--json` mode additionally emits `{"empty_source": true, ...}` on stdout. This fires only on zero *reader* units, **before** the watermark filter — so a captures run whose backlog the watermark has fully drained is a clean success, not an error. The guard lets an external consumer honor a no-silent-lossy-fallback policy: a misconfigured source surfaces, it never silently reports a successful empty run.

## The six score weights

Each candidate is scored on six signals in [0, 1], combined into a weighted total that ranks candidates within a run. The weights follow OpenClaw's Deep Sleep promotion model: `relevance` and `frequency` carry the OpenClaw baseline weight, and the residual is split evenly across the remaining four signals (RESOLUTION 3).

| Signal | Weight | Source |
|---|---|---|
| `relevance` | 0.30 | LLM (qwen3.6:35b-a3b) — how often this signal would help future recall |
| `frequency` | 0.24 | pure — distinct source-event count, linear to a saturation cap |
| `recency` | 0.115 | pure — exponential decay of `event_time` against the run day |
| `importance` | 0.115 | LLM (qwen3.6:35b-a3b) — how much the claim matters long-term |
| `confidence` | 0.115 | LLM (qwen3.6:35b-a3b) — how sure the claim is true |
| `novelty` | 0.115 | embedding (qwen3-embedding:8b) — `1 − max_cosine` vs. existing facts |

The weights **sum to exactly 1.0** (asserted by a test). `relevance` (0.30) and `frequency` (0.24) are the OpenClaw baseline; `0.46` residual ÷ 4 = `0.115` each. The weighted total is the run's ranking signal; it is **not** the promotion decision — the gates below are.

Two novelty notions exist: the **embedding** novelty (`1 − max_cosine` against the day's existing `MEMORY.md` claims, embedded once per run) is the authoritative signal used in the weighted total; the model's own novelty estimate is surfaced in the audit but is not load-bearing. Re-stating a known fact yields a high max-cosine and therefore a low novelty.

## The three promotion gates

`policies/fact-schema.md` §"Gates for promotion" is the **source of truth** for these values. Restated here for the consolidator's convenience:

- `relevance > 0.5`
- `confidence > 0.6`
- `corroboration ≥ 2` — at least two **distinct source events**, OR one explicit **operator-authored** source.

A candidate promotes only when it passes **all three**. The corroboration gate reads the candidate's source **count** (or its operator-authored flag) — **not** the `frequency` weight. The weight shapes the ranking; the count is the gate. The literals live in `palace/consolidate/config.py` as `GATE_RELEVANCE` / `GATE_CONFIDENCE` / `GATE_CORROBORATION`; a test asserts they equal the `fact-schema.md` values exactly, so the two files can never drift.

Every candidate — promoted, discarded, or held — is recorded in `<vault-root>/dreams/DREAMS.md` with its full six-signal breakdown, gate outcomes, and the PROMOTE / DISCARD / HELD verdict. The audit is plain Markdown a headless reader parses; it is append-only.

## Contradiction surfacing (no auto-resolution)

When a candidate that would otherwise promote contradicts an existing `valid: true` fact, the consolidator **holds** it — it does **not** promote the candidate, and it **does not** invalidate, supersede, or overwrite either fact (RESOLUTION 2). The held candidate and the conflicting fact are appended to `<vault-root>/dreams/contradictions/<YYYY-MM-DD>.md` with the rationale and a recommended resolution, for the operator to adjudicate. Auto-resolution of contradicting facts is a deliberate non-feature: facts are invalidated or superseded only by the operator or by an explicit `palace fact` lifecycle command, per `policies/fact-schema.md` §Lifecycle.

Detection is two-stage to bound the slow chat confirmation: an embedding-cosine shortlist of the top-`CONTRADICTION_SHORTLIST_K` (default 5) most-similar existing facts, then a local-chat-model confirmation on each shortlisted pair.

## The run-lock contract

A non-dry-run pass holds an **exclusive** lock at `<store>/meta/consolidator.lock` (or the `--lock-path` override) for its full duration. Acquisition is atomic (`O_CREAT | O_EXCL`); the lock carries `{pid, start_time, host}`. A concurrent invocation that finds a **live** holder refuses with a clear message and a non-zero exit. A **stale** lock — held by a dead pid (`os.kill(pid, 0)` → `ProcessLookupError`) — is reclaimed and acquisition retried once, so a crashed run never deadlocks the next pass. The lock is released (unlinked) on a clean exit.

**A dry-run takes no lock and performs no write.** The dry-run path runs the entire extraction + scoring + gating + contradiction pipeline and returns the same per-candidate breakdown and promote/discard/held decision a real run would, but writes nothing to `MEMORY.md`, `DREAMS.md`, `dreams/contradictions/`, the index, or the events log — and takes no lock. The pipeline core is a pure function of `(llm, embedder, files, clock)` so dry-run ≡ real-run is provable.

## Local-LLM defaults

Per `policies/local-first.md`, extraction and scoring default to **on-device** models on Apple Silicon:

| Step | Model | Endpoint |
|---|---|---|
| Candidate extraction | `qwen3.6:35b-a3b` (Ollama) | `http://localhost:11434` |
| Signal scoring (relevance / importance / confidence / novelty estimate) | `qwen3.6:35b-a3b` (Ollama) | `http://localhost:11434` |
| Novelty similarity (candidate vs. existing facts) | `qwen3-embedding:8b` (Ollama) | `http://localhost:11434` |
| **Contradiction detection** | `qwen3.6:35b-a3b` (Ollama) | `http://localhost:11434` |

The chat model is **`qwen3.6:35b-a3b`** — an A3B mixture-of-experts model (~3B active params/token). On Apple Silicon (M5 Max) it runs multiples faster per call than a dense 31B like `gemma4:31b` while matching or beating it on reasoning and calibration, which is the speed/quality balance the consolidator's cold-path batch wants. It replaces the earlier `gemma4:31b` default. Ollama's XGrammar constrained decoding guarantees schema-valid JSON regardless of model.

Contradiction detection runs on the **local chat model by deliberate choice**, not by oversight. `policies/local-first.md` lists "contradiction resolution" as a *heavy synthesis* task for which a cloud model is acceptable and a local model is the lower-quality-but-acceptable alternative; palace's choice here is the local model, with cloud remaining the documented-quality fallback to reach for deliberately when local quality proves insufficient on this task. The choice is recorded so it reads as considered, not overlooked.

The endpoint honors `$OLLAMA_HOST` (shared with the index embedder's `OLLAMA_BASE_URL`). Switching any model or endpoint is a one-line edit in `palace/consolidate/config.py`.

## Source-trust: the per-kind "authored?" predicate

The corroboration gate admits a single source when that source is **authored** — an explicit, trusted single source. `policies/fact-schema.md` §Gates owns the gate *value* ("`corroboration ≥ 2` — two independent source events, OR one explicit operator-authored source"); this policy owns the **predicate that value maps to**, which is source-kind-aware. The generic flag is `SourceUnit.authored`; how it is populated depends on the source kind:

- **Sessions.** A captured session record is authored when its `harness` is one of the interactive coding harnesses (`claude-code` / `codex`) **AND** its `event_type` is `"stop"` — a top-level turn the operator drove, not a subagent turn or a filesystem event (RESOLUTION 1). This is the operator-authored heuristic from before, unchanged: a deliberately conservative, **tunable** signal that errs toward treating only clearly operator-driven turns as authoritative single sources. Filesystem-change events are never authored. It lives in `palace/consolidate/sources.py::is_wolf_authored` (the helper name is retained; it populates the generic `authored` field for the sessions kind).
- **Captures.** A capture is authored when its frontmatter `source:` begins with one of `USER_TRUST_PREFIXES` (default `("user:",)`) — i.e. a direct user statement (`user:*`) counts, so a single high-value user capture clears the corroboration-via-one-authored-source gate. A `journal:*` source (the consumer's own turn) or a missing `source:` is **not** authored, and so requires ≥ 2 corroborating sources to promote. It lives in `palace/consolidate/captures.py::source_is_user_authored`.

The mapping is consistent with `fact-schema.md` §Gates: that policy's "one explicit operator-authored source" is the sessions-kind reading of the same authored predicate; the gate value is unchanged, only the per-kind predicate that satisfies it is named here. `USER_TRUST_PREFIXES` is a tunable knob in `palace/consolidate/config.py`.

## Transcript resolution

A capture record (Phase 1) stores session **metadata** plus a `transcript_path` pointer — it does **not** inline the conversation. The dialogue itself lives in the referenced Claude Code transcript (`~/.claude/projects/<proj>/<uuid>.jsonl`), so the consolidator **resolves that pointer** to recover the text the extractor reads. `palace/consolidate/sources.py::_resolve_transcript_text` reads the transcript JSONL line-by-line and renders a turn-by-turn `User:` / `Assistant:` dialogue: `user` lines contribute the prompt text, `assistant` lines contribute their `text` content items, and `thinking` / `tool_use` items (and every other line type) are skipped as noise.

The rendered text is capped to `TRANSCRIPT_CHAR_BUDGET` (default `16000`, a knob in `palace/consolidate/config.py`). A whole day's transcript can run past the extractor's useful context, so over-budget text is **head+tail windowed** — the first ~60% (early task framing) plus a `…[truncated]…` marker plus the last ~40% (late conclusions) — so both ends of the conversation survive deterministically.

Resolution is a **graceful fallback**, never a hard requirement. `transcript_path` is the upstream source the capture record *references* — per `storage-layout.md` rule 9 (parametric roots) and `briefs/store-parametric-external-consumers.md`, an external consumer's records may name a different episodic source or carry no transcript at all. When the pointer is falsy, the file is absent or unreadable, or the transcript yields no dialogue, the reader falls back to the record's inline fields (`_session_text`). A transcript line that fails to parse is skipped rather than aborting the run — the transcript is upstream data palace does not own. This keeps an isolated consumer whose records lack transcripts fully runnable.

## All knobs have one home

Every tunable value above — the six weights, the three gate thresholds, the contradiction-shortlist size, the recency half-life, the frequency saturation count, the three model identities, the default source kind, the captures user-trust prefixes, and the capture filename pattern — is declared exactly once, in `palace/consolidate/config.py`. No other module re-declares any of them; tests import from there rather than restating constants. This is the "rules, not memory" invariant applied to the consolidator's parameters: a re-tune is a single audited edit, not a scattered hunt.
