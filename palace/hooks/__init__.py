"""Per-harness hook registration helpers.

``palace.hooks`` houses install / uninstall / status surfaces that wire the
committed hook scripts under ``bin/`` into the per-user settings file of each
supported harness. Phase 1.4 shipped the Claude Code surface
(:mod:`palace.hooks.claude_code`); Phase 1.5 mirrored it for Codex
(:mod:`palace.hooks.codex`).

The package-level :func:`cli_install` / :func:`cli_uninstall` / :func:`status`
shims dispatch by an ``--harness {claude-code,codex,all}`` selector with
default ``all``. Status always reports both harnesses in a single five-block
report (per-harness script + registrations, the shared hook log tail, the
``/health`` probe).
"""

from __future__ import annotations

import sys
from typing import Any

from palace.hooks import claude_code as _cc
from palace.hooks import codex as _codex
from palace.hooks._common import HooksError, hook_log_path, tail

__all__ = ["HARNESSES", "cli_install", "cli_uninstall", "status"]


HARNESSES: tuple[str, ...] = ("claude-code", "codex")


def _selected_harnesses(args: Any) -> list[str]:
    harness = getattr(args, "harness", None) or "all"
    if harness == "all":
        return list(HARNESSES)
    if harness not in HARNESSES:
        raise HooksError(
            f"unknown harness: {harness}; must be one of {sorted([*HARNESSES, 'all'])}"
        )
    return [harness]


def cli_install(args: Any = None) -> int:
    """Register palace's hook scripts in every selected harness's settings file."""
    try:
        targets = _selected_harnesses(args)
    except HooksError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    exit_codes: list[int] = []
    for harness in targets:
        if harness == "claude-code":
            exit_codes.append(_cc.cli_install(args))
        elif harness == "codex":
            exit_codes.append(_codex.cli_install(args))
    return max(exit_codes) if exit_codes else 0


def cli_uninstall(args: Any = None) -> int:
    """Remove palace's hook scripts from every selected harness's settings file."""
    try:
        targets = _selected_harnesses(args)
    except HooksError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    exit_codes: list[int] = []
    for harness in targets:
        if harness == "claude-code":
            exit_codes.append(_cc.cli_uninstall(args))
        elif harness == "codex":
            exit_codes.append(_codex.cli_uninstall(args))
    return max(exit_codes) if exit_codes else 0


def status(_args: Any = None) -> int:
    """Print the unified four-block status report and exit 0.

    Layout (per the Phase 1.5 plan, ordered):

    1. ``-- hook script --`` — the resolved absolute path of each harness's
       hook script (or ``(missing)``).
    2. ``-- registered hooks --`` — the ``claude-code:`` sub-block followed
       by the ``codex:`` sub-block, each listing the registered hook
       commands found in that harness's settings file.
    3. ``-- hook log tail --`` — the last 5 lines of the shared
       ``~/palace-data.noindex/logs/palace-capture-hook.log`` file.
    4. ``-- /health --`` — the loopback daemon's ``/health`` response, or
       ``(unreachable)``.
    """
    from palace.hooks._common import probe_health

    # 1. Resolved hook-script paths.
    print("-- hook script --")
    for harness, module in (("claude-code", _cc), ("codex", _codex)):
        try:
            print(f"{harness}: {module.resolve_hook_script_path()}")
        except HooksError as exc:
            print(f"{harness}: (missing) {exc}")

    # 2. Registered hooks, per harness.
    print()
    print("-- registered hooks --")
    for line in _cc.claude_code_status_block():
        print(line)
    print()
    for line in _codex.codex_status_block():
        print(line)

    # 3. Shared hook log tail.
    print()
    print("-- hook log tail --")
    log_path = hook_log_path()
    if not log_path.exists():
        print("(no hook log yet)")
    else:
        tail_lines = tail(log_path, 5)
        if not tail_lines:
            print("(no hook log yet)")
        else:
            for line in tail_lines:
                print(line)

    # 4. /health probe.
    print()
    print("-- /health --")
    print(probe_health())

    return 0
