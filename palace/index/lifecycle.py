"""LaunchAgent lifecycle for the events-log-consuming index daemon.

Phase 2.5 wraps the foreground-runnable ``palace index serve`` as a per-user
macOS LaunchAgent. The canonical plist lives at
``daemons/index/ai.palace.index.plist``; this module is a thin shim around
:mod:`palace.daemons.launchd`'s shared helpers with the index-specific label
(``ai.palace.index``) and log path
(``~/palace-data.noindex/logs/palace-index.log``) pre-filled.

The post-install confirmation hint explicitly names the Ollama prerequisite
(``qwen3-embedding:8b`` reachable at ``http://localhost:11434``). An installed-
but-immediately-crashing index daemon is the most likely first-time-user trip
wire; surfacing the prerequisite at install time saves the operator a trip to
the log.

``palace index install`` substitutes the three placeholders, writes the
resolved file under the per-user LaunchAgents directory (see
``daemons/index/README.md`` for the absolute path), creates
``~/palace-data.noindex/logs/`` on demand, and bootstraps the agent into the
``gui/<uid>`` domain. ``palace index uninstall`` inverts the install via
``launchctl bootout`` and removes the resolved plist; log history at
``~/palace-data.noindex/logs/palace-index.log`` is preserved across reinstall.
"""

from __future__ import annotations

import sys
from pathlib import Path

from palace.daemons.launchd import (
    LifecycleError,
    install_agent,
    uninstall_agent,
)

__all__ = [
    "PLIST_FILENAME",
    "PLIST_LABEL",
    "PLIST_TEMPLATE_PATH",
    "cli_install",
    "cli_uninstall",
    "install",
    "uninstall",
]


PLIST_LABEL: str = "ai.palace.index"
PLIST_FILENAME: str = "ai.palace.index.plist"
# Repo-relative; the shared installer resolves it only after the macOS guard.
PLIST_TEMPLATE_PATH: Path = Path("daemons") / "index" / PLIST_FILENAME
_LOG_RELATIVE_PATH: str = "palace-data.noindex/logs/palace-index.log"
_POST_INSTALL_HINT: str = (
    "Run 'bash bin/palace-index-status' to confirm it's running. "
    "Ensure Ollama is running and 'qwen3-embedding:8b' is pulled."
)


def install(
    *,
    home: Path | None = None,
    repo_root: Path | None = None,
    launch_agents_dir: Path | None = None,
    uv_bin: Path | None = None,
    run_bootstrap: bool = True,
) -> int:
    """Resolve the plist template and install it as a per-user LaunchAgent."""
    return install_agent(
        label=PLIST_LABEL,
        plist_template_path=PLIST_TEMPLATE_PATH,
        log_relative_path=_LOG_RELATIVE_PATH,
        display_name="index",
        home=home,
        repo_root=repo_root,
        launch_agents_dir=launch_agents_dir,
        uv_bin=uv_bin,
        run_bootstrap=run_bootstrap,
        post_install_hint=_POST_INSTALL_HINT,
    )


def uninstall(
    *,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    run_bootout: bool = True,
) -> int:
    """Bootout the LaunchAgent and remove its installed plist. Idempotent."""
    return uninstall_agent(
        label=PLIST_LABEL,
        plist_filename=PLIST_FILENAME,
        log_relative_path=_LOG_RELATIVE_PATH,
        display_name="index",
        home=home,
        launch_agents_dir=launch_agents_dir,
        run_bootout=run_bootout,
    )


def cli_install() -> int:
    """``palace index install`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return install()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_uninstall() -> int:
    """``palace index uninstall`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return uninstall()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
