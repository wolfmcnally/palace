# daemons/index/

Deployment-artifact home for palace's events-log-consuming index daemon. The Python implementation lives at `palace/index/`; this directory carries the launchd plist and operator notes that supervise the daemon as a per-user `LaunchAgent` on macOS.

## Files

- `ai.palace.index.plist` — templated launchd property list (canonical source). `palace index install` substitutes `{{UV_BIN}}`, `{{HOME}}`, and `{{REPO}}` and writes the resolved file to `~/Library/LaunchAgents/ai.palace.index.plist`. Edit this file, not the installed copy.

All four typed pipelines (Markdown, JSONL, code, opaque text) have landed; see [`palace/index/README.md`](../../palace/index/README.md) for operator documentation of `palace index serve` / `check` / `status`, the chunks DB shape (five tables — the four 2.3 tables plus `jsonl_cursors` for the append-only JSONL pipeline), the per-kind behavior of `frontmatter_json`, and the rebuild procedure.

## What the plist sets and why

- `Label = ai.palace.index` — the service identifier; matches the filename basename.
- `ProgramArguments` — `[uv, run, palace, index, serve, --store, <home>/palace-data.noindex]`. Shell-free explicit exec; `uv run` resolves palace's virtualenv from `WorkingDirectory`. **No `--verbose` flag is passed**: the supervised log should stay steady-state quiet so it doesn't balloon. An operator wanting verbose output runs `palace index serve -v` in the foreground.
- `RunAtLoad = true` — launchd starts the service the moment it bootstraps the agent, including each login.
- `KeepAlive = true` (bare boolean) — any exit (clean or fault) triggers a
  restart. This is precisely why an embedding-identity mismatch parks rather
  than exits: a restart loop could repeatedly contend with the full rebuild
  that repairs the store.
- `ThrottleInterval = 10` (seconds) — caps crash-loop frequency. If Ollama isn't reachable on every restart, launchd waits at least 10s between restarts rather than burning the CPU.
- `ProcessType = Background` — exempt from App Nap; scheduled as a long-running service.
- `StandardErrorPath` and `StandardOutPath` — both pointed at `<home>/palace-data.noindex/logs/palace-index.log`. The index daemon's stderr (startup banner — `probing embedder`, `embedder OK`, `starting from`, `tailing`; lifecycle lines `PARKED` and `RESUMING`; and per-file index lines) flows here.
- `EnvironmentVariables` — `PATH` carries `~/.local/bin` (uv's default install location) plus Homebrew + system bins so `uv run` resolves. `HOME` is set explicitly so `Path.home()` resolves correctly inside the spawned process. `TZ = America/Boise` carries `policies/storage-layout.md` §4's timezone-stable rule into the supervised process so `ingest_time` ISO-8601 stamps carry the home offset regardless of where the laptop is.

The plist deliberately does **not** set `OLLAMA_HOST` or `OLLAMA_BASE_URL` in `EnvironmentVariables`. `palace.index.config.OLLAMA_BASE_URL` defaults to `http://localhost:11434`, which matches the running Ollama process on this host. If a future workload moves Ollama to a non-default host, hand-edit the installed plist's `EnvironmentVariables` block to add `OLLAMA_BASE_URL` and `launchctl kickstart -k gui/$(id -u)/ai.palace.index`. See the Open Questions section below.

- `WorkingDirectory` — the absolute palace repo root, so `uv run` finds `pyproject.toml`.
- `LimitLoadToSessionType = Aqua` — the GUI-session value. The agent only loads in Wolf's interactive login session, matching the per-user `LaunchAgent` posture (not a system-wide `LaunchDaemon`).

The plist deliberately does **not** set `UserName` or `GroupName` — those are `LaunchDaemon`-only keys; a `LaunchAgent` always runs as the owning user.

## Ollama prerequisite

The index daemon embeds via Ollama's `qwen3-embedding:8b` model at `http://localhost:11434/api/embed`. Both must be in place before the daemon can do useful work:

```bash
# Ollama must be installed and running.
brew install ollama          # one-time
ollama serve                 # in a background context (or the macOS GUI app)

# The embedder model must be pulled.
ollama pull qwen3-embedding:8b
```

Verify both:

```bash
curl -sf http://localhost:11434/api/tags >/dev/null && echo ok
ollama list | grep qwen3-embedding
```

**Failure mode without the prerequisite.** A launchd-supervised index daemon whose Ollama isn't running crashes on its probe (`error: ollama unreachable at http://localhost:11434`), restarts per `KeepAlive`, crashes again, and `ThrottleInterval = 10` caps the loop at one restart attempt every ~10 s. The `palace-index.log` accumulates one `error: ollama unreachable ...` line per attempt. The mitigation is "make sure Ollama is running first"; `palace index install` prints this prerequisite as a post-install hint so a fresh operator can't miss it.

## Install / uninstall / status

The plist is loaded into launchd via the modern `bootstrap` / `bootout` verbs, never the deprecated `load` / `unload` pair.

- **Install:** `uv run palace index install` — resolves placeholders, writes the resolved plist to `~/Library/LaunchAgents/ai.palace.index.plist`, creates `~/palace-data.noindex/logs/` on demand, and bootstraps the agent into the `gui/<uid>` domain. Overwrites any prior copy without prompting (greenfield policy). The post-install hint names the Ollama prerequisite.
- **Uninstall:** `uv run palace index uninstall` — runs `launchctl bootout gui/<uid>/ai.palace.index` and removes the installed plist. Idempotent: a second invocation against an already-removed agent exits 0 with an informational "already removed" line. Log history at `~/palace-data.noindex/logs/palace-index.log` is preserved across reinstall.
- **Status:** `bash bin/palace-index-status` — three-block report (launchctl line, last 20 log lines, `palace index status` cursor + chunks-DB report). Exits 0 always; `indexing: ACTIVE` is the health criterion, not mere process liveness.

## Daemon lifecycle

The identity lifecycle is `starting → (parked ⇄ 30-second read-only recheck) →
indexing → clean shutdown`. A writer-readiness timeout instead enters a typed,
terminal park with no automatic retries; correct the reported cause and restart
the daemon. The current state is atomically
published at `<store>/meta/index-daemon-state.json` and removed on clean
shutdown. `palace index status` reports `ACTIVE`, `PARKED`,
`PARKED-REPAIRED`, `STALE`, `STOPPED`, or `UNKNOWN` by composing that file,
process liveness, and the store's four-row embedding identity.

To clear an identity park, run `palace index build --full --store <store>`.
The parked daemon holds no SQLite connection, so no `bootout` is needed. It
auto-resumes within about 30 seconds. If status remains `PARKED-REPAIRED` after
a minute, use `launchctl kickstart -k gui/$(id -u)/ai.palace.index`.
This self-healing behavior applies only to identity parks; a writer-startup park
prints its cause and requires an operator restart after that cause is corrected.

## Operator commands

- **Force a restart:** `launchctl kickstart -k gui/$(id -u)/ai.palace.index` — kills the running instance and immediately respawns it. Useful after a code change, or after starting Ollama.
- **Inspect loaded state:** `launchctl print gui/$(id -u)/ai.palace.index` — full launchd block including current PID, last exit code, and state (`running` / `waiting` / `not running`).
- **Confirm it's healthy:** `bash bin/palace-index-status` — require the
  `indexing: ACTIVE` line; a running PID alone is not sufficient.
- **First-run full-index sweep:** `uv run palace reindex bootstrap` — one-shot foreground walk that enumerates every admitted file under every configured watch root and emits a synthetic `created` event per file into the same events JSONL log this daemon tails. Used to seed the chunks DB on a cold install or after `rm <store>/index/chunks.sqlite`. Canonical doc at [`daemons/reindex/README.md`](../reindex/README.md).
- **Per-file update (`palace index update`):** `uv run palace index update --store <path> --watch-root <root> <path>...` — reflect named files in the index without walking the tree. It shares the per-file core with this daemon and takes the same per-store writer lock (`<store>/meta/index-writer.lock`) around each commit, so update processes, this daemon and a synchronous build may write one store concurrently. Canonical doc in [`palace/index/README.md`](../../palace/index/README.md) §"Concurrent writers and the per-store writer lock".
- **Synchronous build (`palace index build`):** `uv run palace index build [--watch-root <path>] [--store <path>] [--full] [-v]` — the foreground walk-diff-reembed tool that reconciles a tree's chunks DB and exits (no daemon, no events log). It shares the per-file core (`palace.index.core`) with this daemon, so an artifact built by the tool is maintainable by the daemon and vice versa — they agree chunk-for-chunk (the identical-output invariant). Both modes write **watch-root-relative** paths (composite `(watch_root, path)` keys), so a relocated artifact resolves on a backend, and a pre-3.2 absolute-path DB must be destructively rebuilt once (`rm <store>/index/chunks.sqlite` then a `--full` build or `palace reindex bootstrap --force`; no migration code) — see [`palace/index/README.md`](../../palace/index/README.md) §"Relocatable artifact". Canonical doc in [`palace/index/README.md`](../../palace/index/README.md) §"Synchronous build (palace index build)".

See also: [`daemons/reindex/README.md`](../reindex/README.md) for the FSEvents reindex daemon, supervised by its own LaunchAgent per the same pattern.

## Log file

`~/palace-data.noindex/logs/palace-index.log` — startup banner lines plus one `indexed path=... new=N changed=K unchanged=M removed=R` line per file the daemon processes. LF-terminated, UTF-8, single-line records.

`uninstall` does **not** delete this file. To rotate or discard it manually:

```bash
mv ~/palace-data.noindex/logs/palace-index.log ~/palace-data.noindex/logs/palace-index.log.$(date +%Y%m%d)
```

Then `launchctl kickstart -k gui/$(id -u)/ai.palace.index` if the daemon is running, so launchd reopens the file. (macOS launchd opens `StandardErrorPath` on each spawn but not within a single process's lifetime, so a long-lived daemon keeps writing to the rotated file's inode until it next restarts.)

## Spotlight Privacy reminder

Confirm `~/palace-data.noindex/` is in **System Settings → Spotlight → Search Privacy** so Spotlight does not index the machine-state root (which includes the chunks DB at `~/palace-data.noindex/index/chunks.sqlite`). Per `policies/storage-layout.md`, the `.noindex` folder-name suffix already excludes the root from Spotlight on recent macOS; the manual Privacy entry is a belt-and-suspenders confirmation. (The vault at `~/Obsidian/` remains Spotlight-indexed; only the machine-state root is excluded.)

## Open questions deferred

- **`OLLAMA_HOST` / `OLLAMA_BASE_URL` posture.** The 2.5 plist deliberately omits any Ollama environment variable, relying on `palace.index.config.OLLAMA_BASE_URL`'s `http://localhost:11434` default. This matches the current single-machine setup. If Ollama ever moves to a non-default host (a different port, a different machine, a Docker container), add an `OLLAMA_BASE_URL` entry to the installed plist's `EnvironmentVariables` block and `launchctl kickstart -k gui/$(id -u)/ai.palace.index`. Plumbing the env var through the install subcommand is over-engineering until the second host actually exists.
- **No HTTP `/health` endpoint.** The index daemon's API is its cursor + chunks DB; `palace index status` exercises that surface, and the status script's third block prints it. If a future MCP surface or another agent needs a liveness probe, add `/health` in a follow-up sub-phase rather than wedging it into 2.5.
- **Log rotation.** No rotation in 2.5. If `palace-index.log` grows past tens of MB in normal use, revisit by adding a `newsyslog.d` drop-in or a consolidator-owned rotate step. With Ollama down for an extended period the throttled crash-loop fills the log with `error: ollama unreachable` lines at ~10 s cadence; an exponential-backoff posture inside `palace.index.server` is the deeper fix and is out of 2.5's scope.
