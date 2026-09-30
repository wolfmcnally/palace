"""Shared helpers for per-harness hook registration modules.

The Claude Code and Codex install/uninstall/status paths diverge on file
format (JSON vs TOML) but converge on a small set of repo-resolution, hook-log,
health-probe, and tool-discovery utilities. Centralizing those here keeps
:mod:`palace.hooks.claude_code` and :mod:`palace.hooks.codex` strict siblings
with no cross-imports between them.

Public surface (re-exported by the harness modules):

- :exc:`HooksError` — the single exception type the CLI shims translate into a
  one-line stderr diagnostic with exit code 1.
- :data:`BACKUP_SUFFIX` — the ``.palace.bak`` sidecar suffix written next to
  any mutated settings file.
- :func:`resolve_repo_root` — locate the directory containing
  ``pyproject.toml`` ascending from this module.
- :func:`hook_log_path` — the operator-side shared hook log path (honors
  ``PALACE_HOOK_LOG``).
- :func:`require_runtime_tools` — surface a clean error if ``jq`` or ``curl``
  are missing from PATH before mutating anything.
- :func:`health_url_from_capture_env` / :func:`probe_health` / :func:`tail` —
  building blocks for the unified status report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "BACKUP_SUFFIX",
    "DEFAULT_CAPTURE_HEALTH_URL",
    "HooksError",
    "health_url_from_capture_env",
    "hook_log_path",
    "probe_health",
    "require_runtime_tools",
    "resolve_repo_root",
    "tail",
]


BACKUP_SUFFIX: str = ".palace.bak"
DEFAULT_CAPTURE_HEALTH_URL: str = "http://127.0.0.1:8765/health"


class HooksError(Exception):
    """Raised when the install / uninstall / status pipeline cannot proceed.

    Carries a single-line message; the CLI shims translate this into
    ``error: ...`` on stderr and exit 1 without a Python traceback.
    """


def resolve_repo_root() -> Path:
    """Return the palace repo root (the directory containing ``pyproject.toml``)."""
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise HooksError(
        f"could not locate palace repo root (no pyproject.toml ascending from {current})"
    )


def hook_log_path() -> Path:
    """Return the path of the shared hook-side log file.

    Honors ``PALACE_HOOK_LOG`` so the hook scripts and the status helper agree
    on which file to read. Mirrors the env contract in
    ``bin/palace-hook-claude-code`` and ``bin/palace-hook-codex``.
    """
    override = os.environ.get("PALACE_HOOK_LOG")
    if override:
        return Path(override)
    return Path.home() / "palace-data.noindex" / "logs" / "palace-capture-hook.log"


def require_runtime_tools(tools: Iterable[str]) -> None:
    """Raise :class:`HooksError` if any of ``tools`` is missing from PATH.

    The hook scripts depend on ``jq`` and ``curl``. macOS ships ``curl`` by
    default but ``jq`` is a Homebrew install; surfacing the missing tool here
    keeps the operator's first failure mode actionable.
    """
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        names = ", ".join(missing)
        hints = " ".join(f"'brew install {tool}'" for tool in missing)
        raise HooksError(f"required tool(s) not on PATH: {names}; install via {hints}")


def health_url_from_capture_env() -> str:
    """Derive the ``/health`` URL from ``PALACE_CAPTURE_URL`` (or fall back)."""
    capture_url = os.environ.get("PALACE_CAPTURE_URL")
    if not capture_url:
        return DEFAULT_CAPTURE_HEALTH_URL
    # Same scheme + authority, swap path. We intentionally do not use
    # urllib.parse here because PALACE_CAPTURE_URL is operator-controlled and
    # ``/capture`` is the only documented path.
    if capture_url.endswith("/capture"):
        return capture_url[: -len("/capture")] + "/health"
    return DEFAULT_CAPTURE_HEALTH_URL


def probe_health() -> str:
    """Return the body of a ``GET /health`` probe, or ``(unreachable)``."""
    url = health_url_from_capture_env()
    curl = shutil.which("curl")
    if curl is None:
        return "(curl not on PATH)"
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [curl, "-fsS", "--max-time", "2", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return "(unreachable)"
    if result.returncode != 0:
        return "(unreachable)"
    return result.stdout.strip() or "(empty response)"


def tail(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path``, or ``[]`` if it does not exist."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-n:]
