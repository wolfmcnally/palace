# palace.index

Events-log-consuming index daemon. Tails the per-day change-event JSONL
that `palace.reindex` emits, classifies each surviving change as a
typed file kind (markdown / jsonl / code / text / binary / unknown),
runs the matching per-kind pipeline (parse → chunk → re-embed only the
changed chunks via a local Ollama-backed `qwen3-embedding:8b`
embedder), and writes the hybrid retrieval primitives — sqlite-vec +
FTS5 — to `<store>/index/chunks.sqlite`. All four typed pipelines are
live; see "Typed pipelines" below.

## Entry points

```
palace index serve [--store <path>] [-v | --verbose]
palace index build [--store <path>] [--watch-root <path>] [--full]
                   [--embed-concurrency <N>] [--lock-timeout <seconds>]
                   [-v | --verbose] [--json]
palace index update --store <path> --watch-root <root>
                    [--lock-timeout <seconds>] [-v | --verbose] [--json] <path>...
palace index copy --from <store> --to <store> --watch-root <root>
                  [--lock-timeout <seconds>] <path>...
palace index remove [--store <path>] --watch-root <root>
                    [--lock-timeout <seconds>] <path>...
palace index check <path> [--store <path>]
palace index status [--store <path>]
palace index publishing show|set [--store <path>] [--published-corpus --published-corpus-note <text>]
palace index publish-vectors [--store <path>] --bucket <bucket> --index <index>
                             --profile <profile> --region <region> [--text-metadata] [--json]
palace index vector-backend show|set [--store <path>] [--backend sqlite-vec|s3vectors] [--profile <profile>]
```

`palace index serve` runs in the foreground until SIGINT or SIGTERM.
The launchd plist that wraps this as a per-user LaunchAgent shipped in
Phase 2.5 — see [`daemons/index/README.md`](../../daemons/index/README.md)
for `uv run palace index install` / `uninstall` and the operator
`bin/palace-index-status` triage script.

## Prerequisites

The index daemon needs a running [Ollama](https://ollama.ai/) instance
with the `qwen3-embedding:8b` model pulled. The daemon's startup probe
fails loudly if either is missing.

```bash
# Verify Ollama is running:
curl -sf http://localhost:11434/api/tags >/dev/null && echo ok

# Pull the model (one-time, ~5 GB):
ollama pull qwen3-embedding:8b
```

`OLLAMA_HOST` overrides the default base URL; the daemon honors the
same environment variable Ollama's own client uses.

## What the daemon writes

Six tables in one sqlite file at `<store>/index/chunks.sqlite`:

- **`chunks`** — the canonical row store. One row per atomic section
  (or section-window for body-overflow splits). Carries `body`,
  `body_hash`, `heading`, `heading_depth`, `wikilinks_json`,
  `tags_json`, `dataview_fields_json`, `embeds_json`,
  `frontmatter_json`, `ingest_time`, and `kind` (one of `"markdown"
  | "jsonl" | "code" | "text"`). Indexed on `path` and `watch_root`.
- **`chunks_vec`** — sqlite-vec virtual table; `embedding FLOAT[4096]`.
  One row per chunk; same `chunk_id` as the `chunks` row.
- **`chunks_fts`** — FTS5 virtual table over the `body` column with
  the `porter unicode61 remove_diacritics 2` tokenizer. One row per
  chunk; same `chunk_id`.
- **`files`** — per-file state: `file_hash` (SHA-256 of file bytes;
  `"-"` sentinel for JSONL since the cursor below is the equivalent
  identity), `sections_json` (the prior parse's `(section_index,
  window_index, body_hash, chunk_id)` tuples), `last_indexed_at`.
  Composite **`PRIMARY KEY (watch_root, path)`**: `path` is
  watch-root-relative and `watch_root` absolute, so two roots can carry
  the same relative `path` without colliding.
- **`jsonl_cursors`** — per-path byte-offset cursor for the JSONL
  pipeline: `watch_root`, `path`, `last_byte_offset`, `last_indexed_at`,
  with a composite **`PRIMARY KEY (watch_root, path)`** (the `watch_root`
  column was added in 3.2 alongside `files`). Append-only invariant —
  the cursor advances forward only; deletion drops the row alongside the
  chunks.
- **`index_meta`** — the four-row embedding certificate:
  `embed_convention`, `embed_model`, `embed_provider`, and `embed_dim`.
  It lives inside the DB rather than under `<store>/meta/` so the complete
  identity travels with a copied or shipped `chunks.sqlite`.

`chunk_id` is the SHA-256 hex of the canonical-JSON form of
`{body_hash, path, section_index, watch_root, window_index}` — `path` is
watch-root-relative and `watch_root` the absolute root (the salt that
keeps the id cross-root-unique). Recomputable from any committed row
with stdlib tools (key order below is illustrative — `compute_record_id`
sorts keys):

```bash
python3 -c "
import json, hashlib
body = {'body_hash':'<from chunks>','path':'<from chunks>',
        'section_index': <from chunks>, 'window_index': <from chunks>,
        'watch_root':'<from chunks>'}
print(hashlib.sha256(json.dumps(body, sort_keys=True,
      separators=(',',':')).encode()).hexdigest())
"
```

Schema-version marker at `<store>/meta/schema-version` carries the
literal string `chunks-v1` after bootstrap; a mismatch raises
`error: index schema mismatch: ...` at startup. The `jsonl_cursors` and
`index_meta` tables were added additively within `chunks-v1` (no version bump);
no migration shims — the greenfield-until-released policy holds.

## Typed pipelines

Four file kinds index; two short-circuit:

- **`markdown`** — Obsidian-native parse (wikilinks, hierarchical tags,
  YAML frontmatter, Dataview inline fields, embeds); section-chunked
  by heading depth; over-cap sections split into overlapping windows.
  `frontmatter_json` carries the YAML frontmatter dict.
- **`jsonl`** — Append-only tail. The writer reads the cursor from
  `jsonl_cursors`, opens the file at that offset, and parses only the
  newly-appended lines. One JSONL record → one chunk; records that
  exceed `MARKDOWN_MAX_SECTION_CHARS` (4000) fall back to opaque
  line-window splitting via the same engine the `text` pipeline uses.
  When a record carries a 64-char-hex `id` field, that id flows
  through to the chunk id (so retraction events by id can find their
  chunk) and `frontmatter_json` is `{"jsonl_record_id": "<hex>"}`;
  otherwise `frontmatter_json` is NULL. **Append-only is sacred** —
  the cursor advances forward only; palace never re-parses earlier
  bytes of a JSONL file.
- **`code`** — AST-chunked via `py-tree-sitter` for the lean cut
  bundled in 2.4: Python (`.py`), Rust (`.rs`), JavaScript (`.js`),
  TypeScript (`.ts`). One chunk per top-level function / class /
  method (recursing into class/impl bodies); TypeScript adds
  `interface_declaration` and `type_alias_declaration`. The `heading`
  column carries the symbol name. `frontmatter_json` carries
  `{"symbol", "kind", "language", "start_line", "end_line"}`. Every
  other entry in `palace.index.config.CODE_EXTENSIONS` (`.tsx`,
  `.jsx`, `.go`, `.java`, `.kt`, `.swift`, `.c`, `.h`, `.cpp`,
  `.hpp`, `.cs`, `.rb`) falls back to opaque line-window chunking
  with `heading = NULL` and `frontmatter_json = NULL`.
- **`text`** — Opaque line-window chunking. Decodes UTF-8 with
  `errors="replace"`. Suffixes `.txt`, `.text`, `.log`, `.csv`,
  `.tsv` resolve here. `heading = NULL` and `frontmatter_json =
  NULL`.
- **`binary`** / **`unknown`** — Logged and skipped (the cursor
  advances; no DB write). The `binary` short-circuit fires on any
  suffix in `palace.index.config.BINARY_EXTENSIONS` (zip/png/pdf/…);
  the `text` pipeline also runs an in-chunker non-printable-bytes
  heuristic as defense-in-depth.

### `frontmatter_json` polymorphism

The `chunks.frontmatter_json` column carries different shapes per
kind:

- `markdown` — the file's YAML frontmatter dict (or NULL when absent).
- `code` — `{"symbol", "kind", "language", "start_line", "end_line"}`
  for AST chunks; NULL for line-window fall-back chunks.
- `jsonl` — `{"jsonl_record_id": "<64-char hex>"}` when the record's
  `id` field carries that shape; NULL otherwise.
- `text` — always NULL.

This polymorphism stays inside `chunks-v1` per
`policies/greenfield-until-released.md`; a future incompatible
reshaping (split into `markdown_frontmatter_json` /
`code_metadata_json` / `jsonl_record_json`) would earn a
`chunks-v2` rebuild.

### Adding a tree-sitter language

The lean cut in 2.4 ships Python/Rust/JavaScript/TypeScript. To
add a language:

1. Install the per-language PyPI grammar wheel
   (e.g. `tree-sitter-go`).
2. In `palace/index/code.py`, append an entry to `_LANGUAGES` keyed
   by suffix, naming the grammar's `language()` callable, a stable
   language id string, and a per-language extractor function.
3. If the suffix is not already in
   `palace.index.config.CODE_EXTENSIONS`, add it.
4. Add a fixture test under `tests/index/test_code.py`.

`.tsx` ships as line-window fall-back in 2.4 even though
`tree-sitter-typescript` bundles a TSX grammar; wiring it is a
documented two-line follow-up
(`tree_sitter_typescript.language_tsx()` plus a clone of the TS
extractor).

### Binary refusal heuristic

`palace.index.text.looks_binary` examines the first 4096 bytes
(`_TEXT_DETECTOR_SAMPLE_BYTES`):

1. Short-circuit to binary if any NUL byte (`0x00`) appears in the
   sample.
2. Short-circuit to text if the sample is valid UTF-8 (a valid UTF-8
   file with high-byte content is text, not binary).
3. Otherwise, classify as binary when more than 10 %
   (`_BINARY_THRESHOLD`) of bytes fall outside the printable-ASCII
   range plus `\t\n\r`.

## Re-embed only changed sections

The writer parses the file, diffs the new sections against the prior
`files.sections_json` payload (keyed by `(section_index,
window_index)`), and embeds only the new + changed sections. Unchanged
sections keep their existing `chunk_id`; removed sections drop their
chunk row from all three tables; the whole file's transaction is
atomic so a partial parse-or-embed never corrupts the index.

A no-op save (touch without content change) short-circuits before
parsing via the `files.file_hash` SHA-256 — verbose mode logs the
short-circuit as `palace index: noop path=<rel>
reason=file-hash-unchanged`.

## `palace index check`

Inspection-only; reports one of:

- `indexed: <path> kind=<kind> chunks=<N> last_indexed_at=<iso-8601>`
- `not-indexed: <path>` (covered by a watch root but no DB row yet)
- `not-watched: <path>` (no watch root covers it)

Reads the chunks DB read-only. Exits 0 in all three cases; failures
print to stderr and exit 1.

## `palace index status`

The report includes:

```
daemon: <running|stopped> (pid=<PID>, pids=<PID,...>, or pid=none)
cursor: day=<YYYY-MM-DD> byte_offset=<N> last_event_id=<sha256-prefix-12|none>
chunks: <N> rows; <N> markdown; <N> jsonl; <N> code; <N> text; <N> files indexed
embed_convention: <value|UNSTAMPED (run 'palace index build --full')>
embed_model: <value|UNSTAMPED (run 'palace index build --full')>
embed_provider: <value|UNSTAMPED (run 'palace index build --full')>
embed_dim: <value|UNSTAMPED (run 'palace index build --full')>
index_identity: <OK|MISMATCH — axes — writes are refused; run 'palace index build --full'>
indexing: <ACTIVE|PARKED|PARKED-REPAIRED|STALE|STOPPED|UNKNOWN>
last_event_lag: <N> seconds since the latest events-log line was written
```

Read-only; never blocks; exits 0 even when the daemon is stopped. The
`indexing:` line composes the daemon state file, process liveness, and store
identity. A corrupt state file or failed liveness probe reports `UNKNOWN`,
never a false `ACTIVE` or `STOPPED`.

An identity park self-heals after a certified full rebuild. A writer-startup
park is terminal, prints the writer-readiness reason, and never masquerades as
`PARKED-REPAIRED`; correct the cause and restart the daemon.

## Store-level embedder selection

`palace index embedder show --store <store>` prints the selected provider and
the identity a full build would stamp. An absent
`<store>/meta/embedder.toml` means local `qwen3-embedding:8b` through Ollama.
Selection is never inferred from `OPENROUTER_API_KEY`.

An already-*published* corpus — served from internet-reachable infrastructure,
whether or not access is gated — may opt into a pinned OpenRouter upstream. The
criterion is published, not public; see
`policies/local-first.md` § "Public vs.
published".

```bash
palace index embedder set --store <store> \
  --provider openrouter \
  --model Qwen/Qwen3-Embedding-8B \
  --upstream DeepInfra \
  --published-corpus \
  --published-corpus-note 'Published at https://example.test/docs/'
palace index build --full --store <store>
```

The build refuses the default personal store, a watch root overlapping the personal
vault, or an incomplete published-corpus assertion before any network request.
The selector loader refuses any unrecognized key outright, so a selector written
under an earlier field name fails loudly rather than degrading to an unasserted
corpus.
Remote requests are bounded-concurrent, provider-pinned, ZDR-only, and
data-collection denied. Concurrency changes only how many of the same eligible
requests are in flight; it changes neither their contents nor their audit
records. `palace index serve` refuses remote-configured stores because remote
embedding remains synchronous-build-only.

Remote requests are split at `REMOTE_EMBED_MAX_BATCH` (1024) inputs, because
DeepInfra rejects a larger input list with HTTP 422. The split lives in
`OpenRouterEmbedder`, so every call site inherits it; batches are concatenated
in input order and each is its own request, its own audit record, and its own
slice of the chunk-id provenance. Local Ollama documents no such limit and is
not batched.

Remote transport errors, HTTP 429, and every 5xx response receive at most four
attempts with bounded exponential backoff. Non-429 4xx responses, provider
divergence, malformed responses, and dimension mismatches fail immediately.
The startup reachability probe is deliberately single-attempt so an operator
does not wait through the full retry budget before seeing a configuration or
network error.

`palace index build` and `palace index serve` refuse a `chunks.sqlite` whose
tables were written by some other tool rather than crashing partway through the
DDL. Move that database aside, or point `--store` at a directory palace owns.

Every attempted cloud request appends an audit record under
`<store>/events/cloud-egress/YYYY-MM-DD.jsonl`, including failed requests and
synthetic probes. `bin/palace-embed-remote-probe` exercises pin, throughput,
concurrency-curve, sustained-load, and fixture-capture modes; `--store` is
required because its own cloud calls are audited. It is an operator tool, never
part of `./bin/check all`.

## Concurrent writers and the per-store writer lock

Every palace writer of `chunks.sqlite` takes one advisory lock,
`<store>/meta/index-writer.lock`, before it changes the store, so any number
of processes may write one store without coordinating among themselves:

- `palace index build` holds it from before its connection opens until after
  its final stamp, across every root's reconcile and the deletion sweep. A
  second build over the same tree waits, then walks and finds the files
  unchanged, so nothing is embedded twice.
- `palace index update` and the index daemon plan and embed **outside** the
  lock and hold it only to commit one file, so expensive embedding never
  serializes. The daemon also takes it around its startup bootstrap, because
  bootstrapping an empty store stamps its identity.
- A writable metadata connection (`palace metadata replace`,
  `palace.metadata.replace_documents`) holds it for its batch transaction.

The lock is `fcntl.flock` on a local file, and the kernel releases it when
the holder's descriptor closes — including when the holder is killed — so a
crashed writer never leaves a stale lock and there is no reclaim step. It is
not reliable across a network filesystem; keep the store on a local disk.

A waiter polls for the lock until a bounded wait expires: the default is
`WRITER_LOCK_TIMEOUT_SECONDS` (300 s, `palace/writer_lock.py`), and
`--lock-timeout <seconds>` on `build` and `update` (or the `lock_timeout`
library parameter) changes it per call. On timeout the writer fails loudly
with one `error:` line naming the holder — its pid, host, operation and start
time — and writes nothing; it never proceeds unlocked. Because a whole-tree
build holds the lock for its entire reconcile, update callers that run beside
long rebuilds pass a longer wait or schedule around the build.

Every writer connection also declares a SQLite busy timeout
(`WRITER_BUSY_TIMEOUT_SECONDS`, 30 s) and commits open with `BEGIN
IMMEDIATE`, so brief read/commit contention with readers or non-palace
processes waits instead of raising.

**Stale plans are re-planned, never committed.** A plan records the store
state it was computed against (read before the source bytes). At commit,
under the lock, the writer checks that the file still exists or is still
absent as planned and re-reads that store state; if another writer committed
the same file in between, the file reappeared behind a delete plan, or it
vanished behind an index plan, the commit is refused and the file is planned
again against disk and the store as they are now (up to three times per
call). With `-v`, each re-plan logs `replan path=<rel> reason=stale-plan
attempt=<n>`. The result is that the index always reflects the file as some
writer last read it from disk, and a racing update and build, or two updates
of one file, both end with the index matching the file.

The store's recorded embedding identity is also asserted inside every commit
transaction, but a mismatch there is a terminal refusal for that call, not a
re-plan: an update or daemon whose embedder was constructed for the old
identity cannot produce vectors for the new one. Only `palace index build
--full` commits without that assertion, because it holds the lock for its
whole run and replaces the certificate at its end.

## Per-file update (palace index update)

```
palace index update --store <path> --watch-root <root>
                    [--lock-timeout <seconds>] [-v | --verbose] <path>...
```

`palace index update` reflects the named files in the index without walking
the tree: each named file is indexed if present (re-embedding only its
changed sections) or removed from the index if absent. It is the entry point
for a producer that emits files one at a time from many concurrent workers,
each wanting its own file searchable as soon as it lands, while an occasional
`palace index build` still runs as the reconciliation check. The library form
is `palace.index.update.update(store=, watch_root=, paths=, embedder=None,
lock_timeout=, verbose=, log=)`, which returns an `UpdateResult`.

Before any write the call validates every path and refuses the whole call on
the first problem: a path outside the watch root, the watch root itself, a
directory, a `.gitignore` file, or a path the root's ignore rules do not admit
— judged the way the build prunes, so an ignored ancestor directory refuses a
file even when a nested `.gitignore` negates it. The remote-corpus boundary
check applies exactly as for a build. The store must already exist: the update
asserts the recorded embedding identity and **never stamps it**, so a missing
index, an unstamped store or a selector that does not match the recorded
identity refuses and names `palace index build --full`. The update makes no
startup probe; a call whose files are unchanged or deleted makes no embedding
request at all, and the first real request is the reachability check.

One summary line per call, on stderr:

```
palace index update: root=<resolved_root> paths=<N> indexed=<I> removed=<R> unchanged=<U> skipped=<S> replanned=<K> seconds=<T>
```

`indexed` counts present files whose commit changed rows, `removed` absent
files, `unchanged` files the file-hash short-circuit skipped, `skipped` files
the indexer does not chunk (binary, unknown kinds, empty), and `replanned`
plans re-made because another writer committed first. Exit 0 on success; exit
1 with one `error:` line on any refusal, lock timeout, embedder failure or a
plan that went stale three times; exit 2 for a usage error.

## Embedding usage per invocation

Every `palace index build` and `palace index update` totals the embedding requests it made itself, retries and failed attempts included: `requests`, `failed_requests` (no response, or a status of 400 or above), `prompt_tokens` and `cost_usd` (from successful responses, as the provider reports them), and `unknown_cost_requests` (successful responses that reported no cost; any makes `cost_usd` unknown for the invocation). A failed attempt's cost is never read. A build's total includes its startup probe; each root's total covers that root's embeddings only. A local embedder makes no counted requests; the private HTTPS provider's attempts are counted once each, with unknown cost, and reranking is never counted. Concurrent invocations in one process keep separate totals. The totals are not a price list: palace reports only what the provider reports.

The library returns them as `UpdateResult.embedding`, `BuildResult.embedding` (per root) and `BuildReport.embedding` (the invocation); an exception raised after any request carries the usage spent as `embedding_usage`. The summary lines on stderr append `embed_requests=… embed_failed=… prompt_tokens=… cost_usd=…`, and `build` ends with a `total roots=…` line. With `--json`, each command prints exactly one JSON object on stdout: `{"ok": true, …result…, "embedding": {…}}` for an update, `{"ok": true, "roots": […], "embedding": {…}}` for a build, or `{"ok": false, "error": …, "embedding": {…}}` on failure (the exit status stays 1 and the `error:` line stays on stderr). The cloud-egress audit records are unchanged; for each invocation they sum to the same tokens and cost.

## Synchronous build (palace index build)

```
palace index build [--store <path>] [--watch-root <path>] [--full]
                   [--embed-concurrency <N>] [--lock-timeout <seconds>]
                   [-v | --verbose]
```

`palace index build` is the **synchronous, non-daemon** sibling of
`palace index serve`. It walks a source tree, reconciles the chunks DB
to match it (re-embedding only what changed, reconciling deletions),
prints a per-root summary, and exits. The daemon answers "keep this
index current as the world changes"; the tool answers "reconcile this
index to the tree as it is right now, then get out of the way" — the
right shape for generating a project index on demand from a script or a
CI step.

**Two modes over one engine.** Daemon mode (`palace reindex` +
`palace index serve`) reacts to FSEvents live, supervised by launchd.
Synchronous tool mode (`palace index build`) walks to completion and
returns an exit code. Both support full and incremental scopes.

**No events log.** Unlike the daemon, the tool **bypasses the events
log entirely** — it walks the tree and writes the chunks DB directly
(walk → plan → bounded embed stage → ordered single-writer commit → exit). The events JSONL stream
exists only to keep palace's hot FSEvents callback from blocking; a
batch tool has no such constraint, so routing it through the log would
be pure overhead. After a build, `<store>/events/` is absent or empty
and `<store>/meta/index-cursor.json` is never created.

**One bounded pipeline.** The calling thread plans files and commits them in
deterministic walk order; only remote embedding moves to workers. At effective
concurrency 1, embedding runs inline on that same calling thread and no executor
is created. Above 1, the in-flight window is capped at 4,096 chunks (about 67 MB
of float32 vectors at dimension 4,096); one oversize file is admitted alone so
the largest input cannot deadlock the queue. The measured default is 8, the
highest level with a two-hour sustained run. N=16 measured faster in bursts and
remains available through `--embed-concurrency 16`, but is not the default
because it was never sustained. An explicit value above the selected
embedder's measured capability fails loudly; an omitted flag resolves local
Ollama to 1 without refusal.

**Identical output across modes.** A DB built by `palace index build`
over a tree and a DB built by the daemon path (`palace reindex
bootstrap` + `palace index serve`) over the same tree carry **identical
chunk rows** — same `chunk_id`s, bodies, hashes, counts (`ingest_time`
excepted). This is non-negotiable: an artifact built by the tool must be
maintainable by the daemon and vice versa. The invariant is enforced by
a shared per-file core: the parse → file-hash short-circuit →
section-diff → embed-only-changed → transactional upsert → delete logic
lives once in `palace.index.core`. The daemon's `WriterWorker` calls
`palace.index.core.index_one`, which composes planning, inline embedding, and
commit. The synchronous walker composes those same `plan_one` and
`commit_plan` surfaces around its bounded embed stage. There is no parallel
copy, so the identical-output invariant still covers both modes.

**Incremental (default).** Per admitted file, the file-hash short-circuit
skips unchanged bytes (zero embedding) and the section diff re-embeds
only new/changed sections. Then a **deletion-reconciliation sweep**:
every `files`-table path that resolves under the walked root and is no
longer in the on-disk admitted set has its chunks dropped. This is the
step the daemon learns for free from FSEvents `deleted` events and the
tool must compute itself — it is what makes "find the differences"
include "this file is gone."

**Full (`--full`).** Bypasses the file-hash and JSONL-cursor short-circuits,
then deletes each file's prior rows inside the same writer transaction that
commits its replacement. A file that has become unindexable still yields a
delete-only plan, so stale rows cannot survive a full rebuild.

**Ad-hoc `--watch-root`.** `--watch-root <dir>` indexes exactly that
directory even when it is **not** in `watch-roots.toml` — the
project-index-generator ergonomic, so a project can index its own tree
with no configuration. Omitted, the tool walks every configured root.
The same under-store guard `palace watch add` enforces applies
(defense-in-depth: a root resolving under `--store` is refused with
`refuse-root reason=under-store`). An empty config with no `--watch-root`
logs `no watch roots configured; nothing to build` and exits 0.

The per-root summary line:

```
palace index build: root=<resolved_root> files=<N> embedded=<M> unchanged=<K> deleted=<D> embed_concurrency=<C> embed_batching=<shape> seconds=<S>
```

`files` is admitted files walked; `embedded` is files that had at least
one new/changed section re-embedded; `unchanged` is files the file-hash
short-circuit skipped; `deleted` is files dropped by the reconciliation
sweep.

`embed_concurrency` is the effective value after provider capability
resolution. `embed_batching` is provider-accurate:
`per-file(max=1024)` for OpenRouter and `per-file(max=unbounded)` for
Ollama.

**Paths are watch-root-relative (Phase 3.2).** `chunks.path` /
`files.path` / `jsonl_cursors.path` store each file's POSIX path relative
to its watch root (e.g. `src/main.rs`), so the `chunks.sqlite` is
**shippable** — copy it to a backend that lays the tree down at a
different prefix and every path still means something. Both the daemon
and the synchronous tool write the identical relative representation
(the conversion lives once in `palace.index.core`), so the
identical-output invariant holds path-for-path.

The end-to-end smoke is
[`bin/palace-index-build-smoke`](../../bin/palace-index-build-smoke).

### Relocatable artifact

The chunks DB is a **relocatable artifact** — it describes the *tree*,
not the *machine*:

- The three `path` columns (`chunks.path`, `files.path`,
  `jsonl_cursors.path`) store the file's POSIX path **relative to its
  watch root**. None begins with `/` or `~`. Confirm:
  `SELECT DISTINCT substr(path,1,1) FROM chunks` never returns `/` or `~`.
- `chunks.watch_root` / `files.watch_root` carry the **one absolute
  notion of the root**. A single artifact may carry several roots, each
  with its own absolute `watch_root` and N relative paths under it.
- `chunk_id` is salted with the absolute `watch_root` and keys on the
  relative `path`, so a chunk-id is **prefix-independent within a root**
  (the relative path is stable when the tree relocates) yet **distinct
  across roots** (two roots can carry the same relative `README.md`
  without their chunks colliding — they live under distinct
  `(watch_root, path)` keys).
- **Ship it:** `cp <store>/index/chunks.sqlite shipped.sqlite`. Open the
  copy from anywhere; `SELECT path, kind FROM chunks` still shows
  relative, meaningful paths. A backend that lays the tree down at
  `/srv/repos/<proj>/` resolves `src/main.rs` against its own prefix.
- **Local display:** `palace search` resolves the stored relative `path`
  against the stored `watch_root` at display time, so search results
  still surface clickable absolute paths despite the relative storage.
- **Embedding identity:** the copied DB carries all four `index_meta` rows. A
  vector or hybrid reader requires the current convention and dimension plus
  complete model/provider rows. Model/provider values may differ from the
  local query venue because the artifact is already certified uniform. Pure
  BM25 and `expand` do not read vectors and remain available.

**Destructive rebuild of a pre-3.2 DB (operator action, not code).**
A chunks DB written before 3.2 carries **absolute** paths and a
single-column `files` / `jsonl_cursors` primary key; its chunk-ids were
derived from the absolute path. The post-3.2 readers will not resolve
those paths and the post-3.2 chunk-ids will not match, so a pre-3.2 DB
must be **deleted and rebuilt** — there is **no migration code**, no
`chunks-v2`, no dual-read path (per
`policies/greenfield-until-released.md`).
Indexes are derivable, so the rebuild is cheap and lossless. This is a
**one-time operator action**:

```bash
# Synchronous tool:
rm <store>/index/chunks.sqlite
uv run palace index build --full --watch-root <root> --store <store>

# Daemon path:
rm <store>/index/chunks.sqlite \
   <store>/meta/index-cursor.json \
   <store>/meta/schema-version \
   <store>/meta/bootstrap-cursor.json
# with `palace index serve` running:
uv run palace reindex bootstrap --force --store <store>
```

See [`## Rebuild procedure`](#rebuild-procedure) below for the general
derived-index rebuild story.

## Rebuild procedure

The chunks DB is a *derived* view, rebuildable from the canonical files under
each watch root. A convention, model, provider, or dimension change requires a
full re-embed because stored `body_hash` values intentionally do not change.
Rebuild every configured root, with no `--watch-root` filter. A parked daemon
holds no SQLite connection, so it need not be stopped:

```bash
uv run palace index build --full --store <store>
uv run palace index status --store <store>
```

The status output must show `index_identity: OK`; an identity-parked daemon
then resumes within about 30 seconds and reaches `indexing: ACTIVE`. If it remains
`PARKED-REPAIRED` after a minute, use
`launchctl kickstart -k gui/$(id -u)/ai.palace.index`. The build samples
`PRAGMA data_version` on its one
long-lived SQLite connection and checks it again while holding a
`BEGIN IMMEDIATE` transaction. Palace's own writers cannot commit during the
run because the build holds the store's writer lock; if a non-palace
connection commits — or an index daemon left running commits before the lock
was taken — the build completes the vectors but refuses to stamp them and
prints a `not-stamped` line with the rebuild remedy. The store is uncertified,
not corrupt; repeat the full build after resolving the named concurrent
writer. The index daemon's identity-mismatch park is not such a writer and
does not block the remedy.

## First-run posture

In 2.3, the daemon only processes events that arrive *after* it starts.
An operator who configures a watch root pointing at an already-populated
vault sees no chunks until something changes inside that vault. The
Phase 2.6 first-run pass is the answer; the workaround until then is
to edit one file in each watch root to trigger an initial sweep.

## See also

- [`bin/palace-index-smoke`](../../bin/palace-index-smoke) — end-to-end
  smoke against a real Ollama daemon.
- [`bin/palace-index-status`](../../bin/palace-index-status) — operator
  triage script: launchctl line + log tail + `palace index status` block.
- [`palace/reindex/README.md`](../reindex/README.md) — the FSEvents
  producer whose events log this daemon consumes.
- [`daemons/index/README.md`](../../daemons/index/README.md) — the
  deployment-artifact directory; carries the Phase 2.5 launchd plist
  template and the `palace index install` / `uninstall` recipe.
- `plan/phase-2.3.md` — the Markdown
  pipeline + chunks DB.
- `plan/phase-2.4.md` — the typed JSONL /
  code / opaque-text pipelines.
- `briefs/sota-memory-and-recall.md` §B.2 / §C.1 / §C.2 / §G.2
  — Obsidian-native modeling, sqlite-vec, hybrid retrieval, re-embed
  only changed chunks.

## Dependencies

- **`PyYAML>=6,<7`** — Markdown frontmatter parser. MIT-licensed.
- **`sqlite-vec>=0.1.9`** — vec0 virtual table. Already pinned from
  Phase 0.
- **`httpx>=0.27`** — HTTP client for the Ollama embed API. Already
  pinned from Phase 1.1.
- **`tree-sitter>=0.25,<0.26`** — Python bindings for the tree-sitter
  parsing toolkit. MIT-licensed.
- **`tree-sitter-python>=0.23`** — Python grammar. MIT-licensed.
- **`tree-sitter-rust>=0.24`** — Rust grammar. MIT-licensed.
- **`tree-sitter-javascript>=0.23`** — JavaScript grammar.
  MIT-licensed.
- **`tree-sitter-typescript>=0.23`** — TypeScript + TSX grammars.
  MIT-licensed (only TS is wired in 2.4).
- **FTS5** ships with stdlib `sqlite3` on every supported Python 3.12
  build; no new dependency for BM25.
- **Ollama** is a host-level runtime dependency, not a pip package.

## Copying and removing named files (palace index copy / remove)

```
palace index copy --from <store> --to <store> --watch-root <root> [--lock-timeout <seconds>] <path>...
palace index remove [--store <path>] --watch-root <root> [--lock-timeout <seconds>] <path>...
```

`palace index copy` replaces the destination store's rows for each named file with the source store's: its `files` row, chunks, vectors, keyword rows, JSONL cursor and document metadata. Nothing is embedded; chunk ids and vector bytes are preserved, and keyword rows are written afresh from the chunk bodies. A named file the source does not hold is removed from the destination. It serves a consumer that keeps a derived, filtered copy of a store in step one document at a time. The library form is `palace.index.store_ops.copy_paths(source=, destination=, watch_root=, paths=, lock_timeout=)`, returning a `StoreOpResult`.

A copy holds both stores' writer locks, taken in the order of their resolved lock paths, so a source rebuild is never mid-reconcile while the copy reads it and two copies in opposite directions cannot deadlock. It refuses before writing anything when either store's recorded embedding identity is incomplete, when the two identities differ, when the source does not index `--watch-root`, when the destination indexes any other root, when an incoming document id belongs to a different file in the destination, or when source and destination are the same store. Metadata is compared with the destination's state before any write, so repeating an unchanged copy leaves the metadata generation unchanged.

Keyword scores depend on the whole store (BM25 statistics), so a filtered copy ranks by its own corpus: its results match a store indexed from exactly its documents, and match the source's only when it holds the same documents. A copied chunk keeps its keyword rowid whenever the destination has not used that rowid, so chunks with exactly equal keyword scores are ordered as in the source; where a rowid is taken, the copy assigns a new one and such ties may order differently. A source written before the keyword-row lookup existed is converted by the copy under the lock it already holds, like any other writer's first write.

`palace index remove` deletes every row of the named files in one transaction under the store's writer lock; the metadata generation advances only when a metadata row was removed. The library form is `palace.index.store_ops.remove_paths(store=, watch_root=, paths=, lock_timeout=)`.

Paths are relative to the watch root, or absolute under it. Each command prints one summary line (`copied=`, `removed=`, `metadata=changed|unchanged`) and exits nonzero with one `error:` line on refusal.

### The keyword-row lookup

FTS5 cannot index its `UNINDEXED` `chunk_id` column, so finding one chunk's keyword row by id reads the whole keyword table. Every store therefore keeps `chunks_fts_rows(chunk_id, fts_rowid)`, a derived table written with each keyword insert; every per-file update, delete, copy and removal finds a file's keyword rows through it. The same conversion adds an index on `chunks(watch_root, path)`, without which deleting one file's chunks could read every chunk under its root. A store written before the lookup existed is converted once, in one pass and one transaction, by the first writer that opens it (inside the copy's or removal's own transaction when that writer is a copy or removal, so a refused operation leaves the store unconverted); `index_meta` records `fts_rowid_map = complete` afterwards, and a conversion interrupted part-way leaves the store unmarked for the next writer to redo.

## Published vector indexes (palace index publish-vectors)

A store opted in as published (served from cloud infrastructure and queried remotely, public or not; see `policies/local-first.md` § Published vector indexes) can publish its chunk vectors to an Amazon S3 Vectors index in an operator-controlled AWS account, and can select that index as its vector search leg. A deployed consumer then needs neither the vector table in its runtime nor any resource that bills while idle; its keyword leg and chunk rows stay in the local store.

### The opt-in

```
palace index publishing set --store <path> --published-corpus --published-corpus-note "<what is published and where>"
palace index publishing show --store <path>
```

`set` writes `<store>/meta/vector-publishing.toml` with `published_corpus = true`, the note and the time of the assertion. Publishing and querying refuse a store without a complete opt-in. The default personal store and any watch root that overlaps the personal vault (in either direction) are refused whatever the opt-in says; `set` itself refuses the personal store. This opt-in is separate from the OpenRouter embedder's published-corpus assertion: bulk embedding and vector publication are different authorizations, and a store embedded through a private endpoint can still be published.

### Publishing

```
palace index publish-vectors --store <path> --bucket <bucket> --index <index> --profile <profile> --region <region> [--text-metadata] [--json]
```

Each chunk's vector is published under its content-addressed `chunk_id`. A first publish creates the vector bucket and the index when absent: data type float32, the dimension the store's embedding identity records, cosine distance, and `text`, `path` and `heading` declared non-filterable so text metadata can be enabled later. An existing index whose data type, dimension or distance differs is refused before anything is written, and so is one lacking those non-filterable keys when `--text-metadata` is requested. Keys the index lacks are then put, in requests of at most 500 vectors and an estimated request body of at most 18 MiB (under the service's 20 MiB limit), and only after every put succeeds are keys the store no longer holds deleted, so a query never sees a gap. With `--text-metadata`, each vector also carries its chunk's text, watch-root-relative path and heading; a chunk whose metadata exceeds 40,000 bytes refuses the publish before any request. Changing `--text-metadata` republishes every vector. Querying with metadata needs the `s3vectors:GetVectors` permission as well as `s3vectors:QueryVectors`.

The receipt `<store>/meta/vector-publication.json` records the target (region, bucket, index, index ARN, dimension, distance), whether text metadata was published, the embedding identity the vectors were built with, the SHA-256 of the published key set, the vector count and the last publish's put and delete counts. The key set itself is `<store>/meta/vector-publication-keys.txt`, one key per line, sorted; the digest covers exactly its bytes. While a publish is changing the index the receipt reads `state = publishing`; a completed publish rewrites it as `complete`.

A later publish trusts a `complete` receipt for the same index and embedding identity: with an intact key list it diffs the store against that list without listing the index, so an unchanged store makes no put or delete and writes nothing. With a damaged or missing key list it lists the index once and diffs against that. Without a vouching receipt (none, unreadable, another index or identity, or an interrupted publish) it lists the index and republishes every vector, because chunk ids encode neither the embedding identity nor the metadata mode. Changes made to the index outside palace are invisible to a trusted receipt; delete the receipt to make the next publish list and converge. One publisher per store at a time is supported; concurrent publishes to the same store are not guarded.

The library form is `palace.index.s3vectors.publish_vectors(store=, bucket=, index=, region=, profile=, text_metadata=)`, returning a `PublishResult` (`index_arn`, `created_bucket`, `created_index`, `listed_remote`, `put`, `deleted`, `unchanged`, `vector_count`, `text_metadata`, `usage`). The command prints one summary line on stderr; with `--json`, one JSON object on stdout, `{"ok": true, …result…, "usage": {…}}` or `{"ok": false, "error": …, "usage": {…}}`.

### Searching the published index

```
palace index vector-backend set --store <path> --backend s3vectors [--profile <profile>]
palace index vector-backend set --store <path> --backend sqlite-vec
palace index vector-backend show --store <path>
```

`<store>/meta/vector-backend.toml` selects the vector leg of `palace search` and the search library for that store; sqlite-vec remains the default, also when the file is absent. With `s3vectors`, a query is embedded with the store's recorded embedding identity and sent to the index the receipt names, following the service's result pages until the requested pool arrives (the service returns at most 100 results per page). The query refuses before any request when the store is outside the publishing boundary, has no receipt, its last publish did not complete, or the receipt's embedding identity differs from the store's; metadata filters are refused with this leg rather than silently shrinking the pool. The matches feed the existing fusion and reranking unchanged; their distances are cosine distances as the service reports them (the sqlite-vec leg reports its own metric). The service's search is approximate: in the named live check (2026-09-28, a four-vector synthetic index) a top-4 query returned three vectors in exact-cosine order, a top-10 query returned all four, and reported distances were within 0.01 of exact; palace passes on what the service returns and does not pad the pool. A matched chunk with no local row is shown, reranked and merged across stores from its published text metadata when the index carries it. Without `--profile`, the runtime's credential chain is used (for example a function's execution role). Multi-store search resolves each store's own leg.

### Audit and usage

Every S3 Vectors request, publish or query, appends one `cloud_vector_call` record to `<store>/events/cloud-egress/<day>.jsonl`: operation, region, bucket, index and index ARN, the keys a put or delete carried, the input count, the SHA-256 and byte size of the request body, the status (`ok` or the AWS error code), the attempt number and the latency. No chunk text, query text or credential is recorded, and a record that cannot be written refuses the operation, so a query needs a writable store. The SDK makes no retries of its own: palace retries idempotent requests (get, list, put, delete, query) on throttling, unavailability, internal errors, timeouts and connection failures up to four attempts with bounded backoff, and every attempt is a record. Creates are not retried; rerunning the publish converges. A failure to write the receipt or key list after the index changed is reported like any other failure, with the usage spent, and leaves the receipt interrupted so the next publish converges.

The totals (`requests`, `failed_requests`, `request_bytes`, and per-operation counts) are in `PublishResult.usage`, in `RerankedSearch.vector_usage` and `MultiStoreSearch.vector_usage` whenever the index served a search, on a raised error as `vector_usage`, and through `palace.index.s3vectors.vector_usage_scope` for `search()` and `search_expanded()`. `palace search` keeps stdout to its hit lines and reports the totals on stderr after any request, as `palace search: vector backend s3vectors requests=… failed=… request_bytes=…`, or under `--json` as one JSON object `{"vector_usage": {…}}`.

Service limits palace applies come from the pinned service model (the [AWS S3 Vectors API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Operations_Amazon_S3_Vectors.html): dimension up to 4,096, 500 vectors per put or delete, 10 non-filterable keys, 1,000 keys per list page) and from AWS's S3 Vectors limitations and query pages (https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html and https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-query.html, retrieved 2026-09-28: 40 KB of metadata per vector, 2 KB of it filterable, a 20 MiB request payload, top-K up to 10,000, at most 100 results per query page). Pricing (retrieved 2026-09-28): 0.06 USD per GB-month stored, 0.20 USD per GB uploaded, 2.50 USD per million queries, with no minimum or hourly charge.
