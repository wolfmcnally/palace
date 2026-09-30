"""LaunchAgent lifecycle for the FSEvents reindex daemon.

Phase 2.5 wraps the foreground-runnable ``palace reindex serve`` as a per-user
macOS LaunchAgent. The canonical plist lives at
``daemons/reindex/ai.palace.reindex.plist``; this module is a thin shim around
:mod:`palace.daemons.launchd`'s shared helpers with the reindex-specific label
(``ai.palace.reindex``) and log path
(``~/palace-data.noindex/logs/palace-reindex.log``) pre-filled.

``palace reindex install`` substitutes the three placeholders, writes the
resolved file under the per-user LaunchAgents directory (see
``daemons/reindex/README.md`` for the absolute path), creates
``~/palace-data.noindex/logs/`` on demand, and bootstraps the agent into the
``gui/<uid>`` domain. ``palace reindex uninstall`` inverts the install via
``launchctl bootout`` and removes the resolved plist; log history at
``~/palace-data.noindex/logs/palace-reindex.log`` is preserved across
reinstall.
"""

from __future__ import annotations

import sys
from pathlib import Path

from palace.daemons.launchd import (
    LifecycleError,
    install_agent,
    kickstart,
    kickstart_says_not_loaded,
    uninstall_agent,
)

__all__ = [
    "PLIST_FILENAME",
    "PLIST_LABEL",
    "PLIST_TEMPLATE_PATH",
    "RESTART_NOT_LOADED",
    "RESTART_RESTARTED",
    "cli_install",
    "cli_uninstall",
    "install",
    "restart",
    "uninstall",
]


PLIST_LABEL: str = "ai.palace.reindex"
PLIST_FILENAME: str = "ai.palace.reindex.plist"
# Repo-relative; the shared installer resolves it only after the macOS guard.
PLIST_TEMPLATE_PATH: Path = Path("daemons") / "reindex" / PLIST_FILENAME
_LOG_RELATIVE_PATH: str = "palace-data.noindex/logs/palace-reindex.log"

# Outcomes returned by :func:`restart`. Strings (not an enum) to match the
# lightweight status-string convention the rest of the daemon CLIs use.
RESTART_RESTARTED: str = "restarted"
RESTART_NOT_LOADED: str = "not-loaded"


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
        display_name="reindex",
        home=home,
        repo_root=repo_root,
        launch_agents_dir=launch_agents_dir,
        uv_bin=uv_bin,
        run_bootstrap=run_bootstrap,
        post_install_hint="Run 'bash bin/palace-reindex-status' to confirm it's running.",
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
        display_name="reindex",
        home=home,
        launch_agents_dir=launch_agents_dir,
        run_bootout=run_bootout,
    )


def restart() -> str:
    """Kickstart-restart the reindex LaunchAgent so it re-reads watch-roots.

    The daemon loads :class:`palace.watch.config.WatchRootsConfig` exactly
    once at startup (see :mod:`palace.reindex.server`), so a watch-roots
    change only takes effect after the agent restarts. This wraps
    ``launchctl kickstart -k gui/<uid>/ai.palace.reindex`` — the same verb
    ``daemons/reindex/README.md`` documents for a manual restart.

    Returns :data:`RESTART_RESTARTED` when launchctl kickstarted the
    service, or :data:`RESTART_NOT_LOADED` when the agent is not installed
    in the ``gui/<uid>`` domain (the operator must run ``palace reindex
    install`` before any root can be live-watched). Raises
    :class:`LifecycleError` on any other launchctl failure.
    """
    rc, _stdout, stderr = kickstart(PLIST_LABEL)
    if rc == 0:
        return RESTART_RESTARTED
    if kickstart_says_not_loaded(rc, stderr):
        return RESTART_NOT_LOADED
    first_line = next(
        (line for line in stderr.splitlines() if line.strip()),
        f"launchctl kickstart returned {rc}",
    )
    raise LifecycleError(f"launchctl kickstart failed: {first_line}")


def cli_install() -> int:
    """``palace reindex install`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return install()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_uninstall() -> int:
    """``palace reindex uninstall`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return uninstall()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
