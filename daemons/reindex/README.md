# daemons/reindex/

Deployment-artifact home for palace's FSEvents reindexer. The Python implementation lives at `palace/reindex/`; this directory carries the launchd plist, the reference watch-roots example, and the operator notes that supervise the daemon as a per-user `LaunchAgent` on macOS.

## Files

- `ai.palace.reindex.plist` — templated launchd property list (canonical source). `palace reindex install` substitutes `{{UV_BIN}}`, `{{HOME}}`, and `{{REPO}}` and writes the resolved file to `~/Library/LaunchAgents/ai.palace.reindex.plist`. Edit this file, not the installed copy.
- [`watch-roots.example.toml`](watch-roots.example.toml) — placeholder-only; shows the `[[root]]` array-of-tables layout `palace watch add` writes to `~/palace-data.noindex/meta/watch-roots.toml`. The file is structured TOML so a TOML parser reads it as an empty config; uncomment a block and substitute placeholders to hand-edit.

The runtime library lives at [`palace/watch/`](../../palace/watch/) (the watch-roots config + ignore engine + `palace watch` CLI) and [`palace/reindex/`](../../palace/reindex/) (the FSEvents subscription + debouncer + change-event JSONL writer + `palace reindex serve`). See [`palace/watch/README.md`](../../palace/watch/README.md) and [`palace/reindex/README.md`](../../palace/reindex/README.md) for runtime documentation.

## What the plist sets and why

- `Label = ai.palace.reindex` — the service identifier; matches the filename basename.
- `ProgramArguments` — `[uv, run, palace, reindex, serve, --store, <home>/palace-data.noindex]`. Shell-free explicit exec; `uv run` resolves palace's virtualenv from `WorkingDirectory`. **No `--verbose` flag is passed**: the supervised log should stay steady-state quiet so it doesn't balloon. An operator wanting verbose output runs `palace reindex serve -v` in the foreground.
- `RunAtLoad = true` — launchd starts the service the moment it bootstraps the agent, including each login.
- `KeepAlive = true` (bare boolean) — any exit (clean or fault) triggers a restart. The hot-path daemon should never voluntarily exit; every exit is a fault.
- `ThrottleInterval = 10` (seconds) — caps crash-loop frequency. If the daemon wedges on every start, launchd waits at least 10s between restarts rather than burning the CPU.
- `ProcessType = Background` — exempt from App Nap; scheduled as a long-running service.
- `StandardErrorPath` and `StandardOutPath` — both pointed at `<home>/palace-data.noindex/logs/palace-reindex.log`. The reindex daemon's stderr (FSEvents subscription messages, debouncer drops with `-v`, drop-with-reason lines, startup banner) flows here.
- `EnvironmentVariables` — `PATH` carries `~/.local/bin` (uv's default install location) plus Homebrew + system bins so `uv run` resolves. `HOME` is set explicitly so `Path.home()` resolves correctly inside the spawned process. `TZ = America/Boise` carries `policies/storage-layout.md` §4's timezone-stable rule into the supervised process so the daemon's events JSONL writes select today's daily file by Wolf's home timezone regardless of where his laptop is.
- `WorkingDirectory` — the absolute palace repo root, so `uv run` finds `pyproject.toml`.
- `LimitLoadToSessionType = Aqua` — the GUI-session value. The agent only loads in Wolf's interactive login session, matching the per-user `LaunchAgent` posture (not a system-wide `LaunchDaemon`).

The plist deliberately does **not** set `UserName` or `GroupName` — those are `LaunchDaemon`-only keys; a `LaunchAgent` always runs as the owning user.

## Install / uninstall / status

The plist is loaded into launchd via the modern `bootstrap` / `bootout` verbs, never the deprecated `load` / `unload` pair.

- **Install:** `uv run palace reindex install` — resolves placeholders, writes the resolved plist to `~/Library/LaunchAgents/ai.palace.reindex.plist`, creates `~/palace-data.noindex/logs/` on demand, and bootstraps the agent into the `gui/<uid>` domain. Overwrites any prior copy without prompting (greenfield policy).
- **Uninstall:** `uv run palace reindex uninstall` — runs `launchctl bootout gui/<uid>/ai.palace.reindex` and removes the installed plist. Idempotent: a second invocation against an already-removed agent exits 0 with an informational "already removed" line. Log history at `~/palace-data.noindex/logs/palace-reindex.log` is preserved across reinstall.
- **Status:** `bash bin/palace-reindex-status` — three-block report (launchctl line, last 20 log lines, `palace watch list` output). Exits 0 always; failure modes are visible in the printed output.

## Operator commands

- **Force a restart:** `launchctl kickstart -k gui/$(id -u)/ai.palace.reindex` — kills the running instance and immediately respawns it. Useful after a code change.
- **Inspect loaded state:** `launchctl print gui/$(id -u)/ai.palace.reindex` — full launchd block including current PID, last exit code, and state (`running` / `waiting` / `not running`).
- **Confirm it's healthy:** `bash bin/palace-reindex-status` — at-a-glance "is the thing alive right now?" check.

See also: [`daemons/index/README.md`](../index/README.md) for the events-log-consuming index daemon, supervised by its own LaunchAgent per the same pattern.

## Log file

`~/palace-data.noindex/logs/palace-reindex.log` — one line per drop-with-reason (with `-v`) or startup banner. LF-terminated, UTF-8, single-line records.

`uninstall` does **not** delete this file. To rotate or discard it manually:

```bash
mv ~/palace-data.noindex/logs/palace-reindex.log ~/palace-data.noindex/logs/palace-reindex.log.$(date +%Y%m%d)
```

Then `launchctl kickstart -k gui/$(id -u)/ai.palace.reindex` if the daemon is running, so launchd reopens the file. (macOS launchd opens `StandardErrorPath` on each spawn but not within a single process's lifetime, so a long-lived daemon keeps writing to the rotated file's inode until it next restarts.)

## First-run bootstrap

On a cold install the FSEvents daemon catches pure-delta changes only — pre-existing files in a watch root never get indexed until something touches them. `palace reindex bootstrap` closes that gap by enumerating every file the shared `IgnoreEngine` admits and emitting one synthetic `change_kind="created"` event per file into the same events JSONL log the FSEvents daemon writes. The bootstrap is a *second producer* into that log; the Phase 2.3 + 2.4 `palace index serve` consumer is byte-unchanged and consumes the synthetic events through the same dispatch path it uses for FSEvents-driven events.

```
uv run palace reindex bootstrap [--store <path>] [--watch-root <path>] [--force] [-v|--verbose]
```

- Without `--watch-root`, every configured watch root is walked in config order.
- `--watch-root <path>` restricts the walk to one configured root (resolved absolute form; an unconfigured path raises `error: not a configured watch root: …; run 'palace watch add' first`).
- `--force` clears any prior `completed_at` for the named root(s) before walking, so the same files re-emit. The events log is append-only, so `--force` adds new lines without rewriting prior history; the writer's `file_hash` short-circuit in the index daemon recognizes unchanged files and avoids redundant embeddings.
- `--verbose` adds one `palace reindex: bootstrap-emit path=<rel>` line per emit. Without it, the bootstrap is quiet at steady state — one startup line, one per-root summary line per walked root, one shutdown line.

The bootstrap is a **one-shot foreground CLI**, not a daemon — no launchd plist ships with it. The operator invokes it manually after `palace watch add` settles a new watch root or after `rm <store>/index/chunks.sqlite` clears the chunks DB and a rebuild is needed. The supervised long-running daemons remain `palace reindex serve` and `palace index serve` from Phase 2.5.

For the common "add a root and start indexing it now" case, `palace watch add <path> --activate` runs this bootstrap automatically after restarting the daemon, so a single command both registers the root and seeds it — see [`palace/watch/README.md`](../../palace/watch/README.md#--activate). The standalone `palace reindex bootstrap` remains the right tool for re-seeding after a chunks-DB wipe or for walking every configured root at once.

Cursor at `<store>/meta/bootstrap-cursor.json` — top-level JSON dict keyed by the absolute resolved watch-root path. Each entry carries `started_at`, `completed_at`, `last_emitted_relative_path`, `files_emitted`, `files_skipped`. Atomic rewrite via tempfile + `os.replace` every 100 emits, so an interrupted walk loses at most 99 in-flight events on resume. A walk that finds a non-null `completed_at` short-circuits with `palace reindex: root=<root> already bootstrapped at <iso-8601>; pass --force to redo`.

Concurrent with `palace reindex serve`: the two producers share the writer worker's module-level `_events_lock`, so appends never interleave. Running the bootstrap while the FSEvents daemon is also live is a supported configuration — typical when bootstrapping a freshly-installed launchd-supervised setup.


See also: [`palace/reindex/README.md`](../../palace/reindex/README.md) for the module-level documentation and [`bin/palace-reindex-bootstrap-smoke`](../../bin/palace-reindex-bootstrap-smoke) for the end-to-end smoke.

## Spotlight Privacy reminder

Confirm `~/palace-data.noindex/` is in **System Settings → Spotlight → Search Privacy** so Spotlight does not index the machine-state root. Per `policies/storage-layout.md`, the `.noindex` folder-name suffix already excludes the root from Spotlight on recent macOS; the manual Privacy entry is a belt-and-suspenders confirmation. (The vault at `~/Obsidian/` remains Spotlight-indexed; only the machine-state root is excluded.)

## Open questions deferred

- **`OLLAMA_BASE_URL` env-var posture (cross-daemon parity).** The reindex daemon makes no network calls; the `OLLAMA_BASE_URL` question is owned by the index daemon — see [`daemons/index/README.md`](../index/README.md). Mentioned here for cross-daemon parity so the operator's mental model is consistent.
- **Per-daemon-name registry vs per-daemon-subcommand.** 2.5 ships per-daemon `palace reindex install` / `palace index install` / `palace capture install`; an umbrella `palace daemon install <name>` was rejected because it forces a registry maintained out of band with the actual daemon subpackages. Revisit if the per-daemon subcommand count grows past ~10.
- **No HTTP `/health` endpoint.** The reindex daemon's API is its watch-roots view (see `palace watch list`); the status script's third block exercises that surface. If a future MCP surface or another agent needs a liveness probe, add `/health` in a follow-up sub-phase rather than wedging it into 2.5.
- **Log rotation.** No rotation in 2.5. If `palace-reindex.log` grows past tens of MB in normal use, revisit by adding a `newsyslog.d` drop-in or a consolidator-owned rotate step.
