"""Per-user macOS LaunchAgent install/uninstall shared by every palace daemon.

Each daemon contributes a templated plist file under ``daemons/<name>/`` and a
thin lifecycle module that delegates here. Phase 1.3 carved the original shape
against the capture daemon; Phase 2.5 generalized it for the two Phase 2
daemons (reindex + index).

The shared install/uninstall helpers substitute the three templated
placeholders (``{{UV_BIN}}``, ``{{HOME}}``, ``{{REPO}}``), write the resolved
file under the user's per-account LaunchAgents directory (see the per-daemon
README under ``daemons/<name>/`` for the absolute path), and bootstrap the
agent into the user GUI domain via ``launchctl bootstrap``. Uninstall inverts
the install via ``launchctl bootout`` and removes the resolved plist; log
history is preserved across reinstall.

``launchctl bootstrap`` / ``bootout`` are the modern verbs; the older
``load`` / ``unload`` pair is never used. ``bootout`` against an already-
removed service returns non-zero with a recognizable "not find" / "113"
stderr — that branch is treated as informational, not an error, so the
``uninstall`` command is idempotent.

The ``display_name`` parameter on ``install_agent`` / ``uninstall_agent`` is
the operator-facing short name printed in confirmation lines. The capture
wrapper passes ``display_name="capture"``, the reindex wrapper passes
``display_name="reindex"``, the index wrapper passes ``display_name="index"``;
this keeps stdout shapes like ``palace capture installed: <path>`` instead of
the noisier ``palace ai.palace.capture installed: <path>``. When unset, the
fully-qualified ``label`` is used.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "LifecycleError",
    "bootout",
    "bootout_says_not_loaded",
    "bootstrap",
    "install_agent",
    "kickstart",
    "kickstart_says_not_loaded",
    "render_plist",
    "resolve_repo_root",
    "resolve_uv_bin",
    "uninstall_agent",
]


class LifecycleError(Exception):
    """Raised when the install / uninstall pipeline cannot proceed.

    Carries a single-line message; the per-daemon CLI shims translate this
    into ``error: ...`` on stderr and exit 1 without a Python traceback.
    """


def _require_macos() -> None:
    """Refuse launchd operations before resolving paths or changing files."""
    if platform.system() != "Darwin":
        raise LifecycleError("launchd lifecycle operations require macOS")


def _find_repo_root(start: Path) -> Path:
    """Ascend from ``start`` until a directory containing ``pyproject.toml`` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise LifecycleError(
        f"could not locate palace repo root (no pyproject.toml ascending from {start})"
    )


def resolve_uv_bin() -> Path:
    """Locate the ``uv`` binary on the install context's PATH.

    Raises :class:`LifecycleError` with an actionable diagnostic if ``uv`` is
    not on PATH — the launchd-managed daemon execs ``uv run palace ...`` so
    the absolute path of the ``uv`` binary is the load-bearing first
    ``ProgramArguments`` element.
    """
    found = shutil.which("uv")
    if found is None:
        raise LifecycleError(
            "'uv' not found on PATH; install via 'brew install uv' or "
            "'curl -LsSf https://astral.sh/uv/install.sh | sh'"
        )
    return Path(found)


def resolve_repo_root() -> Path:
    """Return the palace repo root (the directory containing ``pyproject.toml``)."""
    return _find_repo_root(Path(__file__).parent)


def render_plist(template: str, *, uv_bin: Path, home: Path, repo: Path) -> str:
    """Substitute the three placeholders in ``template`` and return the result.

    Raises :class:`LifecycleError` if any ``{{...}}`` token remains after the
    substitutions complete — that means the template carries a placeholder
    this function does not know how to resolve.
    """
    rendered = (
        template.replace("{{UV_BIN}}", str(uv_bin))
        .replace("{{HOME}}", str(home))
        .replace("{{REPO}}", str(repo))
    )
    if "{{" in rendered:
        raise LifecycleError(
            "plist template carries unsubstituted '{{...}}' tokens after rendering; "
            "update render_plist to handle the new placeholder"
        )
    return rendered


def bootstrap(plist_path: Path) -> tuple[int, str, str]:
    """Run ``launchctl bootstrap gui/<uid> <plist_path>`` and return (rc, stdout, stderr)."""
    _require_macos()
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            "launchctl",
            "bootstrap",
            f"gui/{os.getuid()}",
            str(plist_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def bootout(label: str) -> tuple[int, str, str]:
    """Run ``launchctl bootout gui/<uid>/<label>`` and return (rc, stdout, stderr).

    Note the target is the *service path* ``gui/<uid>/<label>``, NOT the plist
    path. ``bootout`` against a not-loaded service returns non-zero with a
    "not find" / "113" stderr; callers treat that as informational.
    """
    _require_macos()
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            "launchctl",
            "bootout",
            f"gui/{os.getuid()}/{label}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def kickstart(label: str) -> tuple[int, str, str]:
    """Run ``launchctl kickstart -k gui/<uid>/<label>`` and return (rc, stdout, stderr).

    The ``-k`` flag kills a running instance before respawning, so this is a
    *restart*, not merely a start. The target is the *service path*
    ``gui/<uid>/<label>`` — matching :func:`bootout`, NOT a plist path. A
    service that was never bootstrapped into the domain fails with a "Could
    not find service" stderr; callers treat that via
    :func:`kickstart_says_not_loaded` as informational rather than an error.
    """
    _require_macos()
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/{label}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _stderr_says_not_loaded(rc: int, stderr: str) -> bool:
    """Shared recognizer for the "service not loaded in this domain" branch.

    Both ``bootout`` (against an already-removed service) and ``kickstart``
    (against a never-installed service) surface the same family of
    diagnostics: "Could not find service" (contains "not find"), a "113"
    Mach error, or "No such process". A clean rc is never "not loaded".
    """
    if rc == 0:
        return False
    lowered = stderr.lower()
    return "not find" in lowered or "113" in stderr or "no such process" in lowered


def bootout_says_not_loaded(rc: int, stderr: str) -> bool:
    """Recognize the "service not currently loaded" branch of ``bootout`` failure."""
    return _stderr_says_not_loaded(rc, stderr)


def kickstart_says_not_loaded(rc: int, stderr: str) -> bool:
    """Recognize the "service not loaded" branch of ``kickstart`` failure.

    ``launchctl kickstart`` against an agent that was never bootstrapped
    into the ``gui/<uid>`` domain fails with a "Could not find service"
    diagnostic — the same family :func:`bootout_says_not_loaded` keys on.
    """
    return _stderr_says_not_loaded(rc, stderr)


def install_agent(
    *,
    label: str,
    plist_template_path: Path,
    log_relative_path: str,
    display_name: str | None = None,
    home: Path | None = None,
    repo_root: Path | None = None,
    launch_agents_dir: Path | None = None,
    uv_bin: Path | None = None,
    run_bootstrap: bool = True,
    post_install_hint: str | None = None,
) -> int:
    """Resolve a daemon's plist template and install it as a per-user LaunchAgent.

    All overrides exist so the unit tests can run the substitution and
    file-placement branch without touching the host's real per-user
    LaunchAgents directory or the live ``gui/<uid>`` launchd domain.
    ``run_bootstrap=False`` opts out of the side-effecting ``launchctl`` call
    entirely.

    ``log_relative_path`` is interpreted relative to ``home``; the helper
    creates that path's parent directory so the daemon's first launchd spawn
    does not fail on a missing ``StandardErrorPath`` parent. The helper does
    NOT create the log file itself; macOS launchd opens the file on each
    spawn.

    ``display_name`` is the operator-facing short name used in the post-
    install confirmation line. Defaults to ``label`` when unset.
    """
    _require_macos()
    if not plist_template_path.is_absolute():
        plist_template_path = resolve_repo_root() / plist_template_path
    uv = uv_bin if uv_bin is not None else resolve_uv_bin()
    home_dir = home if home is not None else Path.home()
    repo = repo_root if repo_root is not None else resolve_repo_root()
    agents_dir = (
        launch_agents_dir
        if launch_agents_dir is not None
        else home_dir / "Library" / "LaunchAgents"
    )
    short_name = display_name if display_name is not None else label

    template = plist_template_path.read_text(encoding="utf-8")
    rendered = render_plist(template, uv_bin=uv, home=home_dir, repo=repo)

    (home_dir / log_relative_path).parent.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    installed_path = agents_dir / plist_template_path.name
    installed_path.write_text(rendered, encoding="utf-8")

    if run_bootstrap:
        rc, _stdout, stderr = bootstrap(installed_path)
        if rc != 0:
            first_line = next(
                (line for line in stderr.splitlines() if line.strip()),
                f"launchctl bootstrap returned {rc}",
            )
            raise LifecycleError(f"launchctl bootstrap failed: {first_line}")

    confirmation = f"palace {short_name} installed: {installed_path}"
    if post_install_hint is not None:
        confirmation = f"{confirmation}\n{post_install_hint}"
    print(confirmation, flush=True)
    return 0


def uninstall_agent(
    *,
    label: str,
    plist_filename: str,
    log_relative_path: str,
    display_name: str | None = None,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    run_bootout: bool = True,
) -> int:
    """Bootout a daemon's LaunchAgent and remove its installed plist. Idempotent.

    ``run_bootout=False`` opts out of the side-effecting ``launchctl`` call so
    unit tests can exercise the filesystem branch without touching the live
    launchd domain.

    ``display_name`` is the operator-facing short name used in the final
    confirmation line. Defaults to ``label`` when unset.
    """
    _require_macos()
    home_dir = home if home is not None else Path.home()
    agents_dir = (
        launch_agents_dir
        if launch_agents_dir is not None
        else home_dir / "Library" / "LaunchAgents"
    )
    short_name = display_name if display_name is not None else label

    if run_bootout:
        rc, _stdout, stderr = bootout(label)
        if rc != 0:
            if bootout_says_not_loaded(rc, stderr):
                print(
                    f"{label}: not loaded (already removed)",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                first_line = next(
                    (line for line in stderr.splitlines() if line.strip()),
                    f"launchctl bootout returned {rc}",
                )
                raise LifecycleError(f"launchctl bootout failed: {first_line}")

    installed_path = agents_dir / plist_filename
    if installed_path.exists():
        installed_path.unlink()
    else:
        print(
            f"{label}: plist already absent at {installed_path}",
            file=sys.stderr,
            flush=True,
        )

    log_path = home_dir / log_relative_path
    print(f"Note: log history preserved at {log_path}", flush=True)
    print(f"palace {short_name} uninstalled.", flush=True)
    return 0
