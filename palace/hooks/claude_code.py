"""Claude Code Stop / SubagentStop hook registration in ``~/.claude/settings.json``.

The committed hook script at ``bin/palace-hook-claude-code`` is the runtime
artifact; this module is the small Python surface that registers the script's
absolute path under ``hooks.Stop`` and ``hooks.SubagentStop`` of Claude Code's
per-user settings file. Greenfield-until-released: a stale palace entry is
replaced in place rather than migrated.

The settings file may carry arbitrary foreign content (other hooks, tool
permissions, personal config). All install/uninstall mutations:

- Preserve foreign keys byte-for-byte (no reformatting unrelated structure).
- Replace exactly the palace-owned entry, identified by its ``command``
  ending in ``palace-hook-claude-code``.
- Write atomically via ``tmp + os.replace`` so a partial write never leaves
  the settings file half-mutated.
- Stamp a ``.palace.bak`` of the pre-mutation contents on every install /
  uninstall when there was a prior file, so a malformed mutation can be
  rolled back without git.

The ``claude_code_status_block`` helper produces the lines that go into the
``claude-code:`` section of the unified ``palace hooks status`` report; the
package-level :mod:`palace.hooks` ``status`` shim composes that block with the
Codex block, the hook log tail, and the ``/health`` probe.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from palace.hooks._common import (
    BACKUP_SUFFIX,
    HooksError,
    hook_log_path,
    require_runtime_tools,
    resolve_repo_root,
)

__all__ = [
    "BACKUP_SUFFIX",
    "EVENT_NAMES",
    "HOOK_SCRIPT_NAME",
    "HooksError",
    "claude_code_status_block",
    "claude_settings_path",
    "cli_install",
    "cli_status",
    "cli_uninstall",
    "hook_log_path",
    "install",
    "resolve_hook_script_path",
    "resolve_repo_root",
    "status",
    "uninstall",
]


EVENT_NAMES: tuple[str, ...] = ("Stop", "SubagentStop")
HOOK_SCRIPT_NAME: str = "palace-hook-claude-code"


# --------------------------------------------------------------------- paths


def resolve_hook_script_path() -> Path:
    """Return the absolute path of ``bin/palace-hook-claude-code``.

    Raises :class:`HooksError` if the file is absent or not executable. The
    install subcommand calls this before mutating settings so a broken
    registration never lands.
    """
    path = resolve_repo_root() / "bin" / HOOK_SCRIPT_NAME
    if not path.is_file():
        raise HooksError(f"hook script not found: {path}")
    if not os.access(path, os.X_OK):
        raise HooksError(f"hook script not executable (chmod +x {path}): {path}")
    return path


def claude_settings_path() -> Path:
    """Return the path of Claude Code's per-user settings file.

    Honors the ``PALACE_CLAUDE_SETTINGS_PATH`` env override so the test suite
    can point at a hermetic location under ``tmp_path`` without touching the
    operator's real ``~/.claude/settings.json``.
    """
    override = os.environ.get("PALACE_CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


def _backup_path(settings: Path) -> Path:
    return settings.with_name(settings.name + BACKUP_SUFFIX)


# --------------------------------------------------------------------- I/O


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HooksError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        backup = _backup_path(path)
        raise HooksError(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno} col {exc.colno}); "
            f"restore from {backup} if a prior install left one"
        ) from exc
    if not isinstance(data, dict):
        raise HooksError(f"{path} does not contain a JSON object at the top level")
    return data


def _write_settings_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------- mutation


def _is_palace_entry(entry: Any) -> bool:
    """True iff ``entry`` is a palace-registered hook block.

    A Claude Code hook block is ``{"matcher": ..., "hooks": [{"type": ...,
    "command": ...}, ...]}``. We claim ownership of any entry whose any inner
    command path ends in ``palace-hook-claude-code``.
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for h in hooks:
        if not isinstance(h, dict):
            continue
        command = h.get("command")
        if isinstance(command, str) and command.endswith(HOOK_SCRIPT_NAME):
            return True
    return False


def _palace_entry(script_path: Path) -> dict[str, Any]:
    return {
        "matcher": "*",
        "hooks": [{"type": "command", "command": str(script_path)}],
    }


def _install_into(data: dict[str, Any], script_path: Path) -> dict[str, Any]:
    """Mutate ``data`` in place to register the palace hook; return ``data``.

    Drops any prior palace entry under each event, then appends one fresh
    entry. Foreign entries (any block whose command does not end in
    ``palace-hook-claude-code``) are preserved in order.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    for event in EVENT_NAMES:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        retained = [e for e in existing if not _is_palace_entry(e)]
        retained.append(_palace_entry(script_path))
        hooks[event] = retained
    return data


def _uninstall_from(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Remove palace entries from ``data``; return ``(data, removed_count)``.

    Empty event arrays are pruned. An empty ``hooks`` object is pruned.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data, 0
    removed = 0
    for event in EVENT_NAMES:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            continue
        retained = [e for e in existing if not _is_palace_entry(e)]
        removed += len(existing) - len(retained)
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return data, removed


# --------------------------------------------------------------------- install


def install(
    *,
    settings_path: Path | None = None,
    script_path: Path | None = None,
) -> int:
    """Register the palace Claude Code hook script in ``~/.claude/settings.json``."""
    require_runtime_tools(("jq", "curl"))
    target_settings = settings_path if settings_path is not None else claude_settings_path()
    target_script = script_path if script_path is not None else resolve_hook_script_path()

    target_settings.parent.mkdir(parents=True, exist_ok=True)

    had_prior = target_settings.exists()
    if had_prior:
        shutil.copy2(target_settings, _backup_path(target_settings))

    data = _read_settings(target_settings)
    _install_into(data, target_script)
    _write_settings_atomic(target_settings, data)

    print(
        f"claude-code: registered {target_script}\n"
        f"  in {target_settings} under hooks.Stop and hooks.SubagentStop.\n"
        f"  Start a fresh Claude Code session and watch "
        f"~/palace-data.noindex/sessions/$(TZ=America/Boise date +%F)/ for the JSONL line.",
        flush=True,
    )
    return 0


# --------------------------------------------------------------------- uninstall


def uninstall(*, settings_path: Path | None = None) -> int:
    """Remove the palace Claude Code hook from ``~/.claude/settings.json``.

    Idempotent: a second invocation after a clean uninstall prints
    ``no palace hooks registered`` and exits 0.
    """
    target_settings = settings_path if settings_path is not None else claude_settings_path()

    if not target_settings.exists():
        print(
            f"claude-code: no settings file found at {target_settings}",
            file=sys.stderr,
            flush=True,
        )
        return 0

    shutil.copy2(target_settings, _backup_path(target_settings))
    data = _read_settings(target_settings)
    data, removed = _uninstall_from(data)

    if removed == 0:
        print("claude-code: no palace hooks registered", flush=True)
        return 0

    _write_settings_atomic(target_settings, data)
    print(
        f"claude-code: removed {removed} entr"
        f"{'y' if removed == 1 else 'ies'} from {target_settings}.",
        flush=True,
    )
    return 0


# --------------------------------------------------------------------- status


def _registered_commands(data: dict[str, Any], event: str) -> list[str]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return []
    commands: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)
    return commands


def claude_code_status_block(
    *,
    settings_path: Path | None = None,
) -> list[str]:
    """Return the lines that go into the ``claude-code:`` status sub-block.

    A header line ``claude-code:`` is followed by one line per registered
    ``Stop`` / ``SubagentStop`` command, or a single ``(no hooks registered)``
    line when none are present. The unified ``palace hooks status`` shim
    prints the script path separately, so this block does not repeat it.
    """
    target_settings = settings_path if settings_path is not None else claude_settings_path()

    lines: list[str] = ["claude-code:"]
    if not target_settings.exists():
        lines.append("  (no hooks registered)")
        return lines

    try:
        data = _read_settings(target_settings)
    except HooksError as exc:
        lines.append(f"  (settings unreadable) {exc}")
        return lines

    any_registered = False
    for event in EVENT_NAMES:
        commands = _registered_commands(data, event)
        for command in commands:
            lines.append(f"  {event}: {command}")
            any_registered = True
    if not any_registered:
        lines.append("  (no hooks registered)")
    return lines


# --------------------------------------------------------------------- legacy single-harness status


def status(
    *,
    settings_path: Path | None = None,
    script_path: Path | None = None,
) -> int:
    """Print the Claude-Code-only four-block status report and exit 0.

    The unified Phase 1.5 ``palace hooks status`` shim
    (:func:`palace.hooks.status`) composes this with the Codex block. This
    standalone callable remains available for code that wants the
    Claude-Code-only report — and is what the Phase 1.4 acceptance tests
    exercise unchanged.
    """
    from palace.hooks._common import probe_health

    target_settings = settings_path if settings_path is not None else claude_settings_path()
    log_path = hook_log_path()

    print("-- hook script --")
    try:
        resolved_script = script_path if script_path is not None else resolve_hook_script_path()
        print(str(resolved_script))
    except HooksError as exc:
        print(f"(missing) {exc}")

    print()
    print("-- registered hooks --")
    if not target_settings.exists():
        print("(no hooks registered)")
    else:
        try:
            data = _read_settings(target_settings)
        except HooksError as exc:
            print(f"(settings unreadable) {exc}")
            data = {}
        any_registered = False
        for event in EVENT_NAMES:
            commands = _registered_commands(data, event)
            for command in commands:
                print(f"{event}: {command}")
                any_registered = True
        if not any_registered:
            print("(no hooks registered)")

    print()
    print("-- hook log tail --")
    if not log_path.exists():
        print("(no hook log yet)")
    else:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        tail_lines = lines[-5:]
        if not tail_lines:
            print("(no hook log yet)")
        else:
            for line in tail_lines:
                print(line)

    print()
    print("-- /health --")
    print(probe_health())

    return 0


# --------------------------------------------------------------------- CLI shims


def cli_install(_args: Any = None) -> int:
    try:
        return install()
    except HooksError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_uninstall(_args: Any = None) -> int:
    try:
        return uninstall()
    except HooksError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_status(_args: Any = None) -> int:
    try:
        return status()
    except HooksError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
