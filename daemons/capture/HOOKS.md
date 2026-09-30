# daemons/capture/HOOKS.md

Operator notes for the per-harness hooks that feed the capture daemon. Phase 1.3 installs the daemon as a per-user LaunchAgent; Phase 1.4 wires Claude Code into it via `bin/palace-hook-claude-code` and the `palace hooks {install,uninstall,status}` CLI; Phase 1.5 mirrors that for Codex via `bin/palace-hook-codex`, sharing the install / uninstall / status verbs with an `--harness {claude-code,codex,all}` selector (default `all`).

## What the Claude Code hook does

When a Claude Code turn ends — main thread or subagent — Claude Code invokes registered `Stop` / `SubagentStop` hook commands with the event payload on stdin. The committed hook at `bin/palace-hook-claude-code` is a small POSIX `/bin/sh` + `jq` + `curl` script that:

1. Reads the hook payload from stdin.
2. Bails silently on `stop_hook_active: true` (Claude Code's documented re-entry guard against hook loops).
3. Maps `hook_event_name` to the Phase 1.2 `event_type` discriminator (`Stop` → `stop`, `SubagentStop` → `subagent_stop`).
4. Adds `harness: "claude-code"` and (for SubagentStop) mirrors `transcript_path` into `parent_transcript_path`.
5. POSTs the resulting payload to `${PALACE_CAPTURE_URL:-http://127.0.0.1:8765/capture}` fire-and-forget within a hard 500ms timeout.

No LLM work happens here. No cloud egress. No extraction. The script either lands a single line in the day's session JSONL via the daemon or logs a single LF-terminated warning to `~/palace-data.noindex/logs/palace-capture-hook.log` and exits 0. Claude Code's transcript never sees a hook traceback.

The hook's warm latency budget is < 50ms total; under Apple Silicon with `jq` + `curl` already cached the typical real time measures in single-digit milliseconds.

The live Claude Code hooks spec is documented at <https://code.claude.com/docs/en/hooks>.

**Note on Claude Code's `last_assistant_message`:** the brief at `briefs/sota-memory-and-recall.md` §B.4 names `last_assistant_message` among the fields to capture, but Claude Code's `Stop` hook does **not** surface it on the inbound stdin payload (Codex's `Stop` hook does — see below). Recovering the assistant message for Claude Code sessions is a `transcript_path`-driven follow-up. The Codex side does carry the field on stdin and `bin/palace-hook-codex` remaps it to the canonical `assistant_message` slot.

## What the Codex hook does

When a Codex turn ends, Codex invokes registered `[[hooks.Stop]]` commands with the event payload on stdin. The committed hook at `bin/palace-hook-codex` mirrors the Claude Code script's shape (POSIX `/bin/sh` + `jq` + `curl`, same warm latency budget, same fire-and-forget posture, same hook log) with three Codex-specific behaviors:

1. Codex has no `SubagentStop` event today. Any `hook_event_name` other than `Stop` is logged as `unsupported hook_event_name=<value>` and exits 0; nothing is POSTed. If Codex later adds a subagent-finish event, the script grows a branch in a follow-up sub-phase.
2. Codex's `Stop` payload carries `last_assistant_message`; the script remaps that to the canonical `assistant_message` field name Phase 1.2's `CaptureRecord` uses.
3. `turn_id` is dropped outbound. The daemon's `build_record` ignores unknown payload fields by name; promoting `turn_id` into the schema is a follow-up sub-phase. (The record `id` is computed over the built record, not the inbound payload, so dropping `turn_id` does not affect canonicalization either way.)

The Codex registration lands in `~/.codex/config.toml` (or `$CODEX_HOME/config.toml` if the env var is set) as one `[[hooks.Stop]]` entry without a `matcher` key — the live Codex spec notes "matcher isn't currently used for this event." Round-trip is handled by `tomlkit` so comments and key ordering in operator-authored TOML survive the mutation.

A note on Codex's separate `notify` array: `notify` runs a shell command with no structured stdin and would force the hook script to scrape session state from disk. Palace uses the proper `[[hooks.Stop]]` engine instead.

## Install / uninstall / status

All three subcommands are surface-area on the `palace` CLI and target every supported harness at once by default. The `--harness {claude-code,codex,all}` selector (default `all`) lets the operator target one harness explicitly when wanted.

- **Install:** `uv run palace hooks install` — for each selected harness, resolves the absolute path of the harness's hook script (`bin/palace-hook-claude-code` or `bin/palace-hook-codex`), writes a `.palace.bak` of the current settings file (if it exists), and registers the hook in the harness's per-user settings file. The Claude Code path mutates `~/.claude/settings.json` (JSON), registering under both `hooks.Stop` and `hooks.SubagentStop` with `matcher: "*"`. The Codex path mutates `~/.codex/config.toml` (TOML, round-trip-preserved via `tomlkit`), registering under `[[hooks.Stop]]` without a `matcher` key (the live Codex spec notes "matcher isn't currently used for this event"). Foreign entries are preserved; any stale palace entry is replaced in place per `policies/greenfield-until-released.md`. Each install is atomic (`tmp + os.replace`). A harness whose parent directory is absent is silently skipped with one informational stderr line.
- **Uninstall:** `uv run palace hooks uninstall` — for each selected harness, writes a `.palace.bak`, removes any palace-owned entry, prunes empty event arrays / tables, and writes the file back. Idempotent: a second invocation after a clean uninstall exits 0 with `<harness>: no palace hooks registered`. Treats a missing settings file as informational (stderr line, exit 0).
- **Status:** `uv run palace hooks status` — always reports both harnesses; takes no selector. Prints four blocks (resolved hook-script paths per harness; registered hook commands per harness; tail of `~/palace-data.noindex/logs/palace-capture-hook.log`; result of `GET /health`). Exits 0 always.

## Settings file locations

Claude Code's per-user settings live at `~/.claude/settings.json` (the canonical surface in its settings precedence chain). Project-scoped settings under `<repo>/.claude/settings.json` are an alternative — they would only fire when Claude Code is invoked from inside that project — and are intentionally **not** registered by `palace hooks install`. The per-user default is correct for "capture every Claude Code session as memory."

Codex's per-user config lives at `~/.codex/config.toml` (the documented per-user surface; the `CODEX_HOME` env var overrides the parent directory). Project-scoped `.codex/config.toml` overrides under a repo root are documented by Codex but, like Claude Code's project scope, are intentionally not registered by palace today.

For tests and ad-hoc retargeting:

- `PALACE_CLAUDE_SETTINGS_PATH` overrides the resolved Claude Code settings path.
- `CODEX_HOME` (honored by Codex itself) overrides the Codex home directory; palace honors it transitively.

## `stop_hook_active` semantics

Claude Code sets `stop_hook_active: true` on the inbound hook payload when it has already re-invoked the model after a previous Stop hook returned a non-zero exit (its documented loop guard). The palace hook script reads this flag first; if `true`, the script exits 0 without logging, without POSTing, and without touching the store. Palace defers to the upstream guard rather than inventing a second one. The Codex hook script honors the same flag for parity in case Codex grows an equivalent re-entry mechanism.

## Latency budget

- Warm path (`jq` + `curl` in the OS file cache, daemon healthy): < 50ms real time per invocation.
- Cold path (immediately after `palace capture install`, before any prior hook fire): < 500ms.
- Daemon unreachable: the script honors the same 500ms hard timeout via `curl --max-time 0.5`, then writes one warning line and exits 0.

The hard timeout is what makes the daemon safe to kill: a wedged or missing daemon never blocks Claude Code's transcript.

## Hook log file

`~/palace-data.noindex/logs/palace-capture-hook.log` is the hook-side warning log, shared across all supported harnesses. Distinct from the daemon-side `~/palace-data.noindex/logs/palace-capture.log` (Phase 1.3) because the hook runs in the host agent's process tree under that agent's environment; writing to the same file as the daemon would race the daemon's stderr stream. The split keeps `tail -f` on either file useful.

The single shared file (rather than per-harness logs) keeps `palace hooks status` reading one file and avoids splitting a low-volume signal into N quieter signals. The harness disambiguator is on each line.

Each line is LF-terminated and shaped as:

```
<ISO-8601 UTC timestamp> hook: <harness> <event_type-or-unknown> session=<session_id-or-unknown> <one-line diagnostic>
```

`<harness>` is the literal `claude-code` or `codex` segment so operators can `grep '^.* hook: codex'` for one harness's lines.

Successful POSTs do **not** log here by default — the daemon already logs accept lines into `palace-capture.log`. Set `PALACE_HOOK_LOG_ACCEPTS=1` in the LaunchAgent's environment (or any process that exec's the hook) to opt into accept logging when debugging.

The log path can be overridden via the `PALACE_HOOK_LOG` env var; tests set this to a per-`tmp_path` location so the host log is never touched.

## How to confirm hooks are firing

After `palace hooks install`, start a fresh agent session in either harness, ask a one-line question, and end the session. Then:

```bash
# 1a. Confirm Claude Code registration.
jq '.hooks.Stop[0].hooks[0].command, .hooks.SubagentStop[0].hooks[0].command' \
    ~/.claude/settings.json

# 1b. Confirm Codex registration.
python3 -c "import tomllib, pathlib; \
    d = tomllib.loads(pathlib.Path('${CODEX_HOME:-$HOME/.codex}/config.toml').read_text()); \
    print(d['hooks']['Stop'][0]['hooks'][0]['command'])"

# 2. Confirm a JSONL line landed in today's session directory.
ls -lt ~/palace-data.noindex/sessions/$(TZ=America/Boise date +%F)/ | head -3

# 3. Inspect the most recent file.
LATEST=$(ls -t ~/palace-data.noindex/sessions/$(TZ=America/Boise date +%F)/*.jsonl | head -1)
jq -r '.event_type, .harness, .session_id' "$LATEST" | head -3

# 4. Check the hook log shows no warnings (lines tagged by harness).
tail -10 ~/palace-data.noindex/logs/palace-capture-hook.log 2>/dev/null || echo "(no hook log yet)"

# 5. Confirm the daemon side is alive.
curl -fsS --max-time 2 http://127.0.0.1:8765/health

# 6. One-command status report for both harnesses.
uv run palace hooks status
```

If Claude Code invoked a subagent during the session, the same session file will contain interleaved `stop` and `subagent_stop` records in arrival order; `jq -r '.event_type'` returns the sequence.

## Known properties

- **Loopback only.** The daemon binds `127.0.0.1` only; both hooks post to `127.0.0.1`. No cloud egress, per `policies/local-first.md`. Any local process on the laptop can POST to the daemon — for palace's single-user threat model that's correct, but worth re-opening if palace ever runs for a non-interactive user.
- **No daemon authentication.** Same threat-model context as loopback-only. Re-open if multi-user.
- **Append-only.** The daemon writes; the hooks never touch `sessions/` directly. The hook log is also append-only.
- **Greenfield.** A stale palace entry under either `hooks.Stop` / `hooks.SubagentStop` (Claude Code) or `[[hooks.Stop]]` (Codex) is replaced in place, not migrated.
- **TOML round-trip.** The Codex install path uses `tomlkit` (MIT-licensed; pinned `>=0.13,<1.0`) for round-trip-preserving TOML I/O so operator-authored comments and key ordering survive `palace hooks install`.

## Open questions deferred

- **Project-scoped hooks.** A future `palace hooks install --scope project` could write `<repo>/.claude/settings.json` and `<repo>/.codex/config.toml` instead of the per-user files. Defer until a real use case arrives.
- **Accept-log default.** `PALACE_HOOK_LOG_ACCEPTS` defaults to `0` because the daemon already logs accepts. Toggle to `1` for a session in the LaunchAgent's env to evaluate whether double-logging is useful before flipping the default.
- **SubagentStop parent linkage.** The Claude Code live spec exposes only the subagent's own `transcript_path`; the hook mirrors it into `parent_transcript_path`. If a future spec adds a distinct parent transcript field, the script updates to forward it as a separate field. Codex has no SubagentStop event today.
- **`turn_id` promotion.** Codex's `Stop` payload carries `turn_id`; `bin/palace-hook-codex` drops it outbound because the Phase 1.2 `CaptureRecord` does not yet surface a `turn_id` slot. Promoting it into the schema is a follow-up.
- **Claude Code `last_assistant_message`.** Claude Code's Stop hook stdin does not currently surface the final assistant message; recovering it from `transcript_path` is a follow-up that lives in the consolidator, not the hook.
