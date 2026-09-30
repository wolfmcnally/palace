"""Watch-roots config, ignore engine, and ``palace watch`` CLI.

Phase 2.1 ships three pure-Python primitives that subsequent Phase 2 sub-phases
will attach to:

- :class:`WatchRootsConfig` — the on-disk watch-roots TOML reader / writer
  (`<store>/meta/watch-roots.toml`).
- :class:`IgnoreEngine` — the two-rule ignore engine (universal dotfile
  exclusion + per-directory ``.gitignore`` composition).
- The ``palace watch`` CLI subcommand group (``add`` / ``remove`` / ``list`` /
  ``check``).

No FSEvents subscription, no daemon process, no launchd plist ships here —
those land in Phase 2.2 through 2.6.
"""

from __future__ import annotations

from palace.watch._errors import WatchError, WatchRootsError
from palace.watch.cli import (
    CheckResult,
    cli_add,
    cli_check,
    cli_list,
    cli_remove,
    format_check_result,
)
from palace.watch.config import WatchRoot, WatchRootsConfig, default_config_path
from palace.watch.ignore import IgnoreEngine, is_dotfile_excluded

__all__ = [
    "CheckResult",
    "IgnoreEngine",
    "WatchError",
    "WatchRoot",
    "WatchRootsConfig",
    "WatchRootsError",
    "cli_add",
    "cli_check",
    "cli_list",
    "cli_remove",
    "default_config_path",
    "format_check_result",
    "is_dotfile_excluded",
]
