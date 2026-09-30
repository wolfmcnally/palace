"""Configuration constants for the capture daemon.

These constants are the single source of truth in Phase 1.1; a configuration
file (env / TOML) is deferred to a later sub-phase per the approved plan's
Open Question #1.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = [
    "BOISE_TZ",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STORE",
    "WRITER_QUEUE_MAXSIZE",
]

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8765
DEFAULT_STORE: Path = Path.home() / "palace-data.noindex"
BOISE_TZ: ZoneInfo = ZoneInfo("America/Boise")
WRITER_QUEUE_MAXSIZE: int = 1024
