# daemons/capture/

The Stop-hook capture daemon's deployment artifacts. The Python implementation lives at `palace/daemons/capture/`; this directory carries the launchd plist and operator notes that supervise it as a per-user `LaunchAgent` on macOS.

## Files

- `ai.palace.capture.plist` — templated launchd property list (canonical source). `palace capture install` substitutes `{{UV_BIN}}`, `{{HOME}}`, and `{{REPO}}` and writes the resolved file to `~/Library/LaunchAgents/ai.palace.capture.plist`. Edit this file, not the installed copy.

## What the plist sets and why

- `Label = ai.palace.capture` — the service identifier; matches the filename basename.
- `ProgramArguments` — `[uv, run, palace, capture, serve, --host, 127.0.0.1, --port, 8765, --store, <home>/palace-data.noindex]`. Shell-free explicit exec; `uv run` resolves palace's virtualenv from `WorkingDirectory`.
- `RunAtLoad = true` — launchd starts the service the moment it bootstraps the agent, including each login.
- `KeepAlive = true` (bare boolean) — any exit (clean or fault) triggers a restart. The hot-path daemon should never voluntarily exit; every exit is a fault.
- `ThrottleInterval = 10` (seconds) — caps crash-loop frequency. If the daemon wedges on every start, launchd waits at least 10s between restarts rather than burning the CPU.
- `ProcessType = Background` — exempt from App Nap; scheduled as a long-running service.
- `StandardErrorPath` and `StandardOutPath` — both pointed at `<home>/palace-data.noindex/logs/palace-capture.log`. Phase 1.1's writer emits one stderr line per accepted/rejected POST; under launchd those lines land in this file. Any accidental stdout is also captured rather than dropped.
- `EnvironmentVariables` — `PATH` carries `~/.local/bin` (uv's default install location) plus Homebrew + system bins so `uv run` resolves. `HOME` is set explicitly so `Path.home()` resolves correctly inside the spawned process. `TZ = America/Boise` carries `policies/storage-layout.md` §4's timezone-stable rule into the supervised process so daily session directories stay stable across travel.
- `WorkingDirectory` — the absolute palace repo root, so `uv run` finds `pyproject.toml`.
- `LimitLoadToSessionType = Aqua` — the GUI-session value. The agent only loads in Wolf's interactive login session, matching the per-user `LaunchAgent` posture (not a system-wide `LaunchDaemon`).

The plist deliberately does **not** set `UserName` or `GroupName` — those are `LaunchDaemon`-only keys; a `LaunchAgent` always runs as the owning user.

## Install / uninstall / status

The plist is loaded into launchd via the modern `bootstrap` / `bootout` verbs, never the deprecated `load` / `unload` pair.

- **Install:** `uv run palace capture install` — resolves placeholders, writes the resolved plist to `~/Library/LaunchAgents/ai.palace.capture.plist`, creates `~/palace-data.noindex/logs/` on demand, and bootstraps the agent into the `gui/<uid>` domain. Overwrites any prior copy without prompting (greenfield policy).
- **Uninstall:** `uv run palace capture uninstall` — runs `launchctl bootout gui/<uid>/ai.palace.capture` and removes the installed plist. Idempotent: a second invocation against an already-removed agent exits 0 with an informational "already removed" line. Log history at `~/palace-data.noindex/logs/palace-capture.log` is preserved across reinstall.
- **Status:** `bash bin/palace-capture-status` — three-block report (launchctl line, last 20 log lines, `/health` response). Exits 0 always; failure modes are visible in the printed output.

## Operator commands

- **Force a restart:** `launchctl kickstart -k gui/$(id -u)/ai.palace.capture` — kills the running instance and immediately respawns it. Useful after a code change.
- **Inspect loaded state:** `launchctl print gui/$(id -u)/ai.palace.capture` — full launchd block including current PID, last exit code, and state (`running` / `waiting` / `not running`).
- **Confirm it's healthy:** `bash bin/palace-capture-status` — at-a-glance "is the thing alive right now?" check.

See also: [`daemons/reindex/README.md`](../reindex/README.md) and [`daemons/index/README.md`](../index/README.md) for the FSEvents reindex daemon and the events-log-consuming index daemon respectively, both supervised by their own LaunchAgents per the same Phase 1.3 → 2.5 IaC pattern.

## Log file

`~/palace-data.noindex/logs/palace-capture.log` — one line per accepted or rejected POST, plus the daemon's startup banner and shutdown signal lines. LF-terminated, UTF-8, single-line records.

`uninstall` does **not** delete this file. To rotate or discard it manually:

```bash
mv ~/palace-data.noindex/logs/palace-capture.log ~/palace-data.noindex/logs/palace-capture.log.$(date +%Y%m%d)
```

Then `launchctl kickstart -k gui/$(id -u)/ai.palace.capture` if the daemon is running, so launchd reopens the file. (macOS launchd opens `StandardErrorPath` on each spawn but not within a single process's lifetime, so a long-lived daemon keeps writing to the rotated file's inode until it next restarts.)

## Open questions deferred

- **Log rotation.** No rotation in 1.3. If `palace-capture.log` grows past tens of MB in normal use, revisit by adding a `newsyslog.d` drop-in or a consolidator-owned rotate step.
- **Port collision.** `8765` is hard-coded in the plist template. If a future palace daemon wants the same port, this becomes a coordination point. Defer until a second daemon actually wants 8765.
- **Multi-user posture.** Single-user `LaunchAgent` under `gui/<uid>`. If palace ever needs to run for a non-interactive user or a different account, that's a new design conversation, not a parameter on this plist.
