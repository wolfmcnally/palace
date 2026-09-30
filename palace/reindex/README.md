# palace.reindex

FSEvents-driven change-event daemon. Subscribes to every watch root in the
watch-roots config (managed via `palace watch`), debounces 100 ms per path,
filters every event through the shared `IgnoreEngine`, and appends one
structured change-event line per surviving change to
`<store>/events/<YYYY-MM-DD>.jsonl`.

## Entry point

```
palace reindex serve [--store <path>] [-v | --verbose]
```

Runs in the foreground until SIGINT or SIGTERM. The launchd plist that
wraps this as a per-user LaunchAgent shipped in Phase 2.5 — see
[`daemons/reindex/README.md`](../../daemons/reindex/README.md) for
`uv run palace reindex install` / `uninstall` and the operator
`bin/palace-reindex-status` triage script.

## What the daemon emits

One JSON object per line in `<store>/events/<YYYY-MM-DD>.jsonl`, UTF-8,
LF-terminated, sorted-key canonical form on disk. Schema lives in
[`palace/reindex/schema.py`](schema.py); every field is always present
(absent values are `null`). Example:

```json
{"change_kind":"created","event_type":"fs_change","id":"<sha256>",
 "ingest_time":"2026-05-18T14:30:00-06:00","is_directory":false,
 "observed_at":"2026-05-18T14:29:59-06:00","path":"<absolute-path-to-vault>/notes.md",
 "relative_path":"notes.md","rename_dest_path":null,"rename_src_path":null,
 "watch_root":"<absolute-path-to-vault>"}
```

The `id` is the SHA-256 hex of the canonical-JSON form of every other
field — byte-identical to the capture daemon's id idiom from Phase 1.1.
Any line can recompute its `id` from the rest of the record via:

```bash
jq -c '. | del(.id)' <file> | head -1 | python3 -c \
  "import json,hashlib,sys; d=json.loads(sys.stdin.read()); \
   print(hashlib.sha256(json.dumps(d,sort_keys=True,\
   separators=(',',':')).encode()).hexdigest())"
```

## Debouncing

A per-path 100 ms window collapses bursts of events targeting the same
`(watch_root, relative_path)` into a single emitted record per the five
coalescing rules in [`palace/reindex/debounce.py`](debounce.py):

- `created + modified` → `created` (file is new from the consumer's view).
- `created + deleted` → drop (file flickered out before any pipeline
  could index it).
- `deleted + created` → `modified` (atomic-rename idiom at the same path).
- `modified + modified` → `modified`.
- Rename events emit two records (`deleted` at source, `created` at
  destination) and are never coalesced with same-path modifications.

The 100 ms window is hard-coded; tests construct
`PathDebouncer(interval=0.010)` directly rather than via a CLI flag.

## `--store` / `$PALACE_STORE` precedence

Mirrors the rule Phase 2.1's `palace watch` settled on:

1. `--store <path>` (CLI flag); takes the value verbatim.
2. `$PALACE_STORE` (environment variable); fallback for test isolation
   and the eventual reindexer plist's `EnvironmentVariables`.
3. `~/palace-data.noindex/` (the canonical default).

## Bootstrap walk

`palace reindex bootstrap` is the cold-start complement to the FSEvents daemon. It enumerates every file under every configured watch root the shared `IgnoreEngine` admits via `os.walk(root, topdown=True, followlinks=False)` with sorted `dirnames` + `filenames`, prunes ignored subtrees in-place (so `.git/` disappears in O(1), not O(depth)), and emits one synthetic `change_kind="created"` `ChangeEvent` per admitted file through the same `WriterWorker` and module-level `_events_lock` the FSEvents daemon uses. The Phase 2.3 + 2.4 `palace index serve` consumer is byte-unchanged — it cannot tell a bootstrap-emitted event from an FSEvents-driven event, by design.

Cursor file `<store>/meta/bootstrap-cursor.json` — JSON dict keyed by resolved absolute watch-root path. Each entry: `started_at`, `completed_at`, `last_emitted_relative_path`, `files_emitted`, `files_skipped`. Atomic rewrite via tempfile + `os.replace` every 100 emits (`BOOTSTRAP_CHECKPOINT_INTERVAL`). A walk that finds a non-null `completed_at` short-circuits unless `--force` is passed.

`--force` clears the cursor entry on entry and re-walks. The events log is append-only — `--force` adds new lines, never rewrites prior history. The index daemon's `file_hash` short-circuit means unchanged files do not re-embed, so a `--force` re-run is cheap in chunks-DB terms.

See [`daemons/reindex/README.md`](../../daemons/reindex/README.md) for the operator-facing CLI shape and the 30-minute Apple-Silicon performance target.

## Triage — "why didn't I see an event?"

`palace watch check <path>` answers this in one command. The daemon and
`watch check` share the same `IgnoreEngine` instance, so a path that
`check` reports as `ignored: ... (reason: dotfile|gitignore)` is the same
path the daemon refuses to emit a change event for. If the two disagree,
that is a bug.

## Spotlight

`~/palace-data.noindex/` ends in `.noindex` and is therefore
Spotlight-excluded automatically (the suffix is honored by macOS without
any GUI step). The Obsidian vault at `~/Obsidian/` stays
Spotlight-indexed by default. For any additional watch root the operator
should make a deliberate per-root decision — palace does not touch
Spotlight Privacy on the operator's behalf. See
`policies/storage-layout.md` for the
rationale.

## Deferred-optimization notes

- **Config is consulted at daemon start only.** `palace watch add` while
  the daemon is running does not affect the running observer; restart
  the daemon to pick up new roots
  (`launchctl kickstart -k gui/$(id -u)/ai.palace.reindex` once the
  Phase 2.5 LaunchAgent is installed). SIGHUP reload is deferred to a
  later sub-phase.
- **`.gitignore` change events are silently dropped in 2.2.** The
  `IgnoreEngine.refresh()` wire (so a `.gitignore` mutation invalidates
  the cache and re-reads from disk) is a later sub-phase — the engine
  still walks parents on every `is_ignored()` call today.
- **Writer is log-and-continue on unreadable events files.** A single
  failed write surfaces as a stderr line and the worker keeps draining
  the queue. A loud-failure mode (exit non-zero so launchd's KeepAlive
  catches the fault) is tracked as a future hardening pass; 2.5 wired
  the LaunchAgent without changing this posture.
- **Queue-full drops are logged at WARN.** A burst that fills the writer
  queue (default 4096 entries, 1 s put timeout) drops the overflowing
  records with `palace reindex: drop reason=queue-full path=<rel>` on
  stderr. The line prints regardless of `--verbose` — this is a real
  drop the operator must see.

## See also

- `plan/phase-2.2.md` — the phase that shipped
  this.
- [`palace/reindex/schema.py`](schema.py) — change-event field set.
- [`palace/watch/`](../watch/) — the watch-roots config and ignore engine
  this daemon consumes.
- `policies/storage-layout.md` — the
  append-only invariant for `events/` and the America/Boise day-boundary
  rule.
- `briefs/sota-memory-and-recall.md` §B.1
  — FSEvents subscription, 100 ms debounce, change-event JSONL.
