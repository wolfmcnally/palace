"""LaunchAgent lifecycle for the capture daemon.

Phase 1.3 wraps the foreground-runnable ``palace capture serve`` as a per-user
macOS LaunchAgent. The canonical plist lives at
``daemons/capture/ai.palace.capture.plist``; this module reads it, substitutes
the three templated placeholders (``{{UV_BIN}}``, ``{{HOME}}``, ``{{REPO}}``),
writes the resolved file under the user's per-account LaunchAgents directory
(see ``daemons/capture/README.md`` for the absolute path), and bootstraps the
agent into the user GUI domain via ``launchctl bootstrap``. ``uninstall``
inverts the install via ``launchctl bootout`` and removes the resolved plist;
log history is preserved across reinstall.

Phase 2.5 hoisted the daemon-agnostic helpers (placeholder substitution,
bootstrap/bootout invocation, the not-loaded recognizer) to
:mod:`palace.daemons.launchd` and reduced :func:`install` /
:func:`uninstall` to thin wrappers around :func:`install_agent` /
:func:`uninstall_agent` with the capture-specific arguments pre-filled
(notably ``display_name="capture"``, which keeps Phase 1.3's
``palace capture installed: <path>`` / ``palace capture uninstalled.``
stdout byte-identical). The module re-exports ``LifecycleError``,
``render_plist``, ``resolve_repo_root``, and ``resolve_uv_bin`` from the
shared module so direct callers from 1.3 continue to import them by
their previous names; the launchctl-verb helpers
(``bootstrap`` / ``bootout`` / ``bootout_says_not_loaded``) are not
re-exported because 1.3's callers reached them only through ``install`` /
``uninstall``, both of which now delegate through the shared agent
helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

from palace.daemons.launchd import (
    LifecycleError,
    install_agent,
    render_plist,
    resolve_repo_root,
    resolve_uv_bin,
    uninstall_agent,
)

__all__ = [
    "PLIST_FILENAME",
    "PLIST_LABEL",
    "PLIST_TEMPLATE_PATH",
    "LifecycleError",
    "cli_install",
    "cli_uninstall",
    "install",
    "render_plist",
    "resolve_repo_root",
    "resolve_uv_bin",
    "uninstall",
]


PLIST_LABEL: str = "ai.palace.capture"
PLIST_FILENAME: str = "ai.palace.capture.plist"
# Repo-relative; the shared installer resolves it only after the macOS guard.
PLIST_TEMPLATE_PATH: Path = Path("daemons") / "capture" / PLIST_FILENAME
_LOG_RELATIVE_PATH: str = "palace-data.noindex/logs/palace-capture.log"


def install(
    *,
    home: Path | None = None,
    repo_root: Path | None = None,
    launch_agents_dir: Path | None = None,
    uv_bin: Path | None = None,
    run_bootstrap: bool = True,
) -> int:
    """Resolve the plist template and install it as a per-user LaunchAgent.

    All overrides exist so the unit tests can run the substitution and
    file-placement branch without touching the host's real per-user
    LaunchAgents directory or the live ``gui/<uid>`` launchd domain.
    ``run_bootstrap=False`` opts out of the side-effecting ``launchctl`` call
    entirely.
    """
    return install_agent(
        label=PLIST_LABEL,
        plist_template_path=PLIST_TEMPLATE_PATH,
        log_relative_path=_LOG_RELATIVE_PATH,
        display_name="capture",
        home=home,
        repo_root=repo_root,
        launch_agents_dir=launch_agents_dir,
        uv_bin=uv_bin,
        run_bootstrap=run_bootstrap,
        post_install_hint="Run 'bash bin/palace-capture-status' to confirm it's running.",
    )


def uninstall(
    *,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    run_bootout: bool = True,
) -> int:
    """Bootout the LaunchAgent and remove its installed plist. Idempotent.

    ``run_bootout=False`` opts out of the side-effecting ``launchctl`` call
    so unit tests can exercise the filesystem branch without touching the
    live launchd domain.
    """
    return uninstall_agent(
        label=PLIST_LABEL,
        plist_filename=PLIST_FILENAME,
        log_relative_path=_LOG_RELATIVE_PATH,
        display_name="capture",
        home=home,
        launch_agents_dir=launch_agents_dir,
        run_bootout=run_bootout,
    )


def cli_install() -> int:
    """``palace capture install`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return install()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_uninstall() -> int:
    """``palace capture uninstall`` entry point. Translates errors to exit 1 + stderr."""
    try:
        return uninstall()
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
