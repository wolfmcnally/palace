# palace.watch

Watch-roots config, ignore engine, and the `palace watch` CLI.

Phase 2.1 ships three pure-Python primitives that subsequent Phase 2 sub-phases
will build on:

1. **Watch-roots config** — on-disk TOML at `<store>/meta/watch-roots.toml`
   (canonically `~/palace-data.noindex/meta/watch-roots.toml`). One
   `[[root]]` array-of-tables entry per watch root, with `path = "<absolute
   path>"` and an optional `added_at = "<ISO-8601>"` timestamp.
2. **Ignore engine** — the two rules from
   `briefs/sota-memory-and-recall.md` §B.1:
   - **Universal dotfile exclusion.** Any path component starting with `.`
     is excluded at every depth (`.git/`, `.obsidian/`, `.DS_Store`,
     `.vscode/`, `.envrc`, …). Not configurable.
   - **Per-directory `.gitignore` composition.** Honored across the watch
     root, parent-first with child overrides, via
     [`pathspec.GitIgnoreSpec`](https://github.com/cpburnz/python-pathspec).
     Applies whether or not the root is a git repository.
3. **`palace watch` CLI** — four subcommands that mutate or inspect the
   above (`add` / `remove` / `list` / `check`).

No FSEvents subscription, no daemon, no launchd plist ships in Phase 2.1 —
those land in Phase 2.2 through 2.6.

## CLI

Every subcommand accepts `--store <path>` defaulting to
`$PALACE_STORE` (or `~/palace-data.noindex/` if unset). The flag takes
precedence when both are present.

### `palace watch add <path>`

Resolves `<path>` to an absolute path, validates per the rules below, appends
a `[[root]]` entry to the config, and prints `watch: added <absolute-path>`.

Validation rules (any failure exits 1 with a single-line stderr `error:`
message, no Python traceback):

1. The path must exist on disk.
2. The path must be a directory.
3. The path must not be the `--store` root itself or a subpath of it (the
   self-write-loop guard).
4. The path must not equal an existing watch root (duplicate).
5. The path must not be a strict ancestor or strict descendant of an
   existing watch root (overlap).

#### `--activate`

By default `add` only writes the config; the running reindex daemon loads
watch-roots once at startup, so a freshly-added root is ignored until the
daemon next restarts and its existing files are never seeded. `--activate`
closes that gap in one command:

1. **Restart the reindex daemon** via `launchctl kickstart -k
   gui/<uid>/ai.palace.reindex` so its FSEvents observer schedules the new
   subtree for live changes. Prints `watch: reindex daemon restarted`.
2. **Bootstrap-walk the new root** (`palace reindex bootstrap --watch-root
   <path>`, in-process) so the files already on disk are emitted as
   synthetic `created` events and seeded into the index. Prints
   `watch: activated <absolute-path>`.

Order is restart-then-bootstrap on purpose: once the observer is live, any
edit that lands during the walk is also caught by FSEvents, and the index
pipeline's content-hash short-circuit makes the duplicate harmless.

The index daemon (`ai.palace.index`) is **not** touched — it tails the
shared events log and is watch-root-agnostic, so it consumes the new root's
events without a restart.

Edge cases (both non-fatal — the bootstrap still seeds existing files):

- **Daemon not installed.** If `ai.palace.reindex` is not loaded in the
  `gui/<uid>` domain, `--activate` warns `reindex daemon not loaded; live
  updates need 'palace reindex install'` on stderr and continues with the
  bootstrap. Live updates begin once the daemon is installed.
- **Custom `--store`.** The installed daemon runs against the canonical
  `~/palace-data.noindex/`. With a non-default `--store`, `--activate`
  **skips the restart entirely** (restarting the canonical daemon would
  touch an unrelated service and still not live-watch a root under a
  different store), warns `--store … is not the canonical store`, and seeds
  the files only.

### `palace watch remove <path>`

Symmetric. Matches by resolved absolute form so the CLI accepts either the
original argument shape (`~`-prefixed or relative) or the canonicalized
absolute path. Prints `watch: removed <absolute-path>` on success.

### `palace watch list`

Prints one absolute path per line, in insertion order. An entry whose
`path` no longer resolves to a directory is suffixed with
`  (missing on disk)` (two-space separator). An absent or empty config
prints `(no watch roots configured)`.

### `palace watch check <path>`

Inspection-only. Resolves `<path>` to an absolute path, identifies which
watch root it falls under (if any), and prints one of:

- `indexed: <path> (root: <root-path>)`
- `ignored: <path> (root: <root-path>, reason: dotfile)`
- `ignored: <path> (root: <root-path>, reason: gitignore)`
- `not-watched: <path>`

Exits 0 in all four cases — this is an inspection tool, not a check.
Failure modes (config unreadable, malformed TOML) emit a single-line stderr
`error:` line and exit 1.

## How to confirm a path's classification

Whenever Wolf wants to ask "why isn't palace seeing this file?",
`palace watch check <path>` is the one-command answer. The `reason: dotfile`
vs `reason: gitignore` distinction surfaces exactly which rule excluded the
path so the next move is unambiguous (drop the dotfile component, edit a
`.gitignore`, or add a new watch root that covers it).

## On-disk shape

```toml
# palace watch-roots config.
# Generated and updated by `palace watch add` / `palace watch remove`.
# See daemons/reindex/README.md for the Phase 2 reindexer that consumes this.

[[root]]
path = "<absolute-path-to-obsidian-vault>"
added_at = "2026-05-18T14:30:00-06:00"

[[root]]
path = "<absolute-path-to-a-dev-project>"
added_at = "2026-05-18T14:31:12-06:00"
```

The committed reference shape lives at
[`daemons/reindex/watch-roots.example.toml`](../../daemons/reindex/watch-roots.example.toml).

## See also

- `plan/phase-2.1.md` — the phase that shipped this.
- `plan/phase-2.md` — parent phase; the FSEvents
  reindexer that consumes `palace.watch`.
- `policies/storage-layout.md` — why the
  config lives under `~/palace-data.noindex/meta/`.
- `briefs/sota-memory-and-recall.md` §B.1
  — the ignore-engine rules.
