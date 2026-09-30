"""Codex `Stop` hook registration in ``~/.codex/config.toml``.

The committed hook script at ``bin/palace-hook-codex`` is the runtime artifact;
this module is the small Python surface that registers the script's absolute
path under ``[[hooks.Stop]]`` in Codex's per-user TOML config file. Codex's
config file may carry arbitrary foreign content (other hooks, ``notify``
arrays, model defaults, personal config), so all mutations:

- Use :mod:`tomlkit` for round-trip-preserving TOML I/O so comments, key
  ordering, and whitespace survive the mutation byte-for-byte.
- Replace exactly the palace-owned entry, identified by a nested ``command``
  ending in ``palace-hook-codex``. Per
  ``policies/greenfield-until-released.md``, replace; do not migrate.
- Write atomically via ``tmp + os.replace`` so a partial write never leaves
  the config file half-mutated.
- Stamp a ``.palace.bak`` of the pre-mutation contents on every install /
  uninstall **when there was a prior file** (a from-scratch install has
  nothing to back up). Matches Phase 1.4's posture.

Codex's hook engine documents `matcher` as a regex against an event-specific
selector for tool events (e.g. `^Bash$` for `PreToolUse`); the live docs
explicitly note "matcher isn't currently used for this event" for `Stop`, so
the entry omits the `matcher` key entirely.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import AoT, Array, Table

from palace.hooks._common import (
    BACKUP_SUFFIX,
    HooksError,
    require_runtime_tools,
    resolve_repo_root,
)

__all__ = [
    "EVENT_NAMES",
    "HOOK_SCRIPT_NAME",
    "HooksError",
    "cli_install",
    "cli_uninstall",
    "codex_config_path",
    "codex_home",
    "codex_parent_present",
    "codex_status_block",
    "install",
    "resolve_hook_script_path",
    "uninstall",
]


EVENT_NAMES: tuple[str, ...] = ("Stop",)
HOOK_SCRIPT_NAME: str = "palace-hook-codex"


# --------------------------------------------------------------------- paths


def resolve_hook_script_path() -> Path:
    """Return the absolute path of ``bin/palace-hook-codex``.

    Raises :class:`HooksError` if the file is absent or not executable. The
    install subcommand calls this before mutating the config so a broken
    registration never lands.
    """
    path = resolve_repo_root() / "bin" / HOOK_SCRIPT_NAME
    if not path.is_file():
        raise HooksError(f"hook script not found: {path}")
    if not os.access(path, os.X_OK):
        raise HooksError(f"hook script not executable (chmod +x {path}): {path}")
    return path


def codex_home() -> Path:
    """Return the Codex home directory.

    Honors ``CODEX_HOME`` per Codex's documented override; defaults to
    ``~/.codex/``.
    """
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def codex_config_path() -> Path:
    """Return the path of Codex's per-user config file."""
    return codex_home() / "config.toml"


def codex_parent_present() -> bool:
    """True iff the resolved Codex home directory exists.

    The install path treats an absent parent as "Codex was never installed on
    this machine" and silently skips with an informational stderr line.
    """
    return codex_home().is_dir()


def _backup_path(config: Path) -> Path:
    return config.with_name(config.name + BACKUP_SUFFIX)


# --------------------------------------------------------------------- I/O


def _read_config(path: Path) -> TOMLDocument:
    """Parse the TOML config file with comments / whitespace preserved.

    Treats an absent or empty file as an empty document so the install path
    can write a fresh file from scratch.
    """
    if not path.exists():
        return tomlkit.document()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HooksError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return tomlkit.document()
    try:
        return tomlkit.parse(text)
    except Exception as exc:  # tomlkit raises a family of parse errors
        backup = _backup_path(path)
        raise HooksError(
            f"{path} is not valid TOML ({exc}); restore from {backup} if a prior install left one"
        ) from exc


def _write_config_atomic(path: Path, doc: TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = tomlkit.dumps(doc)
    if not serialized.endswith("\n"):
        serialized += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------- mutation


def _is_palace_entry(entry: Any) -> bool:
    """True iff ``entry`` is a palace-registered Codex hook block.

    Codex's `[[hooks.Stop]]` is an array-of-tables; each entry's
    `[[hooks.Stop.hooks]]` carries the actual command bindings. We claim
    ownership of any entry whose any inner command path ends in
    ``palace-hook-codex``.
    """
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    for h in inner:
        if not isinstance(h, dict):
            continue
        command = h.get("command")
        if isinstance(command, str) and command.endswith(HOOK_SCRIPT_NAME):
            return True
    return False


def _palace_entry(script_path: Path) -> Table:
    """Build the `[[hooks.Stop]]` table for palace's hook.

    Omits the `matcher` key — the live Codex spec notes matcher isn't used
    for `Stop`. The inner `[[hooks.Stop.hooks]]` table carries
    `type = "command"` plus the absolute script path.
    """
    entry = tomlkit.table()
    inner_aot = tomlkit.aot()
    inner = tomlkit.table()
    inner.add("type", "command")
    inner.add("command", str(script_path))
    inner_aot.append(inner)
    entry.add("hooks", inner_aot)
    return entry


def _hooks_table(doc: TOMLDocument) -> Table:
    """Return the `[hooks]` table, creating it if absent."""
    existing = doc.get("hooks")
    if isinstance(existing, Table):
        return existing
    table = tomlkit.table()
    doc["hooks"] = table
    return table


def _install_into(doc: TOMLDocument, script_path: Path) -> TOMLDocument:
    """Mutate ``doc`` in place to register the palace Codex hook.

    Foreign entries under `[[hooks.Stop]]` are preserved in order; any prior
    palace entry is removed before the new entry is appended (replace, don't
    migrate).
    """
    hooks = _hooks_table(doc)

    existing_stop = hooks.get("Stop")
    if isinstance(existing_stop, AoT):
        retained = [e for e in existing_stop if not _is_palace_entry(e)]
        new_aot = tomlkit.aot()
        for entry in retained:
            new_aot.append(entry)
        new_aot.append(_palace_entry(script_path))
        hooks["Stop"] = new_aot
    else:
        new_aot = tomlkit.aot()
        new_aot.append(_palace_entry(script_path))
        hooks["Stop"] = new_aot
    return doc


def _uninstall_from(doc: TOMLDocument) -> tuple[TOMLDocument, int]:
    """Remove palace entries from ``doc``; return ``(doc, removed_count)``.

    An emptied `[[hooks.Stop]]` array is pruned; an emptied `[hooks]` table is
    also pruned so a clean uninstall leaves the config file structurally
    minimal.
    """
    hooks = doc.get("hooks")
    if not isinstance(hooks, Table):
        return doc, 0
    existing_stop = hooks.get("Stop")
    if not isinstance(existing_stop, (AoT, Array, list)):
        return doc, 0
    removed = 0
    retained = []
    for entry in existing_stop:
        if _is_palace_entry(entry):
            removed += 1
        else:
            retained.append(entry)
    if retained:
        new_aot = tomlkit.aot()
        for entry in retained:
            new_aot.append(entry)
        hooks["Stop"] = new_aot
    else:
        del hooks["Stop"]
    # Prune `[hooks]` if it ends up empty.
    if not list(hooks.keys()):
        del doc["hooks"]
    return doc, removed


# --------------------------------------------------------------------- install


def install(
    *,
    config_path: Path | None = None,
    script_path: Path | None = None,
) -> int:
    """Register the palace Codex hook script in ``~/.codex/config.toml``."""
    require_runtime_tools(("jq", "curl"))
    target_config = config_path if config_path is not None else codex_config_path()
    target_script = script_path if script_path is not None else resolve_hook_script_path()

    if not target_config.parent.is_dir():
        # Codex has never been installed on this machine. Silently skip with a
        # single informational stderr line. Greenfield-until-released: do not
        # create `~/.codex/` on the operator's behalf.
        print(
            f"codex: no parent directory found at {target_config.parent}; skipping",
            file=sys.stderr,
            flush=True,
        )
        return 0

    had_prior = target_config.exists()
    if had_prior:
        shutil.copy2(target_config, _backup_path(target_config))

    doc = _read_config(target_config)
    _install_into(doc, target_script)
    _write_config_atomic(target_config, doc)

    print(
        f"codex: registered {target_script}\n"
        f"  in {target_config} under [[hooks.Stop]].\n"
        f"  Start a fresh Codex session and watch "
        f"~/palace-data.noindex/sessions/$(TZ=America/Boise date +%F)/ for the JSONL line.",
        flush=True,
    )
    return 0


# --------------------------------------------------------------------- uninstall


def uninstall(*, config_path: Path | None = None) -> int:
    """Remove the palace Codex hook from ``~/.codex/config.toml``.

    Idempotent: a second invocation after a clean uninstall prints
    ``codex: no palace hooks registered`` and exits 0.
    """
    target_config = config_path if config_path is not None else codex_config_path()

    if not target_config.exists():
        print(
            f"codex: no config file to mutate at {target_config}",
            file=sys.stderr,
            flush=True,
        )
        return 0

    shutil.copy2(target_config, _backup_path(target_config))
    doc = _read_config(target_config)
    doc, removed = _uninstall_from(doc)

    if removed == 0:
        print("codex: no palace hooks registered", flush=True)
        return 0

    _write_config_atomic(target_config, doc)
    print(
        f"codex: removed {removed} entr{'y' if removed == 1 else 'ies'} from {target_config}.",
        flush=True,
    )
    return 0


# --------------------------------------------------------------------- status


def _registered_commands(doc: TOMLDocument) -> list[str]:
    hooks = doc.get("hooks")
    if not isinstance(hooks, Table):
        return []
    entries = hooks.get("Stop")
    if not isinstance(entries, (AoT, Array, list)):
        return []
    commands: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, (AoT, Array, list)):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)
    return commands


def codex_status_block(
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Return the lines that go into the ``codex:`` status sub-block.

    A header line ``codex:`` is followed by one line per registered ``Stop``
    command, or one of the markers ``(no config file)`` / ``(no hooks
    registered)`` when either the file is absent or the Stop array is empty.
    The unified ``palace hooks status`` shim prints the script path
    separately, so this block does not repeat it.
    """
    target_config = config_path if config_path is not None else codex_config_path()

    lines: list[str] = ["codex:"]
    if not target_config.exists():
        lines.append("  (no config file)")
        return lines

    try:
        doc = _read_config(target_config)
    except HooksError as exc:
        lines.append(f"  (config unreadable) {exc}")
        return lines

    commands = _registered_commands(doc)
    if not commands:
        lines.append("  (no hooks registered)")
        return lines
    for command in commands:
        lines.append(f"  Stop: {command}")
    return lines


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
