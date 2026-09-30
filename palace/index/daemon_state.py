"""Durable run-state publication for the index daemon."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from palace.daemons.capture.config import BOISE_TZ

__all__ = [
    "DaemonRunState",
    "DaemonState",
    "ParkKind",
    "StateUnreadable",
    "clear_state",
    "daemon_state_path",
    "new_state",
    "read_state",
    "write_state",
]


DaemonRunState = Literal["starting", "indexing", "parked"]
ParkKind = Literal["identity", "writer-startup"]


@dataclass(frozen=True, slots=True)
class DaemonState:
    """One observable index-daemon lifecycle state."""

    state: DaemonRunState
    pid: int
    since: str
    park_kind: ParkKind | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class StateUnreadable:
    """A present state file that cannot be trusted."""

    path: Path
    reason: str


def daemon_state_path(store: Path) -> Path:
    """Return ``<store>/meta/index-daemon-state.json``."""
    return store / "meta" / "index-daemon-state.json"


def new_state(
    state: DaemonRunState,
    *,
    park_kind: ParkKind | None = None,
    reason: str | None = None,
) -> DaemonState:
    """Construct a state for this process using the canonical clock."""
    return DaemonState(
        state=state,
        pid=os.getpid(),
        since=datetime.now(BOISE_TZ).isoformat(timespec="seconds"),
        park_kind=park_kind,
        reason=reason,
    )


def write_state(store: Path, state: DaemonState) -> None:
    """Atomically replace the daemon-state file."""
    file_path = daemon_state_path(store)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), sort_keys=True, separators=(",", ":"))
    temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        dir=str(file_path.parent),
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
    )
    try:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
    finally:
        temporary.close()
    os.replace(temporary.name, file_path)


def read_state(store: Path) -> DaemonState | StateUnreadable | None:
    """Read the state, distinguishing absence from present-but-corrupt."""
    file_path = daemon_state_path(store)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        state = raw["state"]
        pid = raw["pid"]
        since = raw["since"]
        park_kind = raw["park_kind"]
        reason = raw.get("reason")
        if state not in {"starting", "indexing", "parked"}:
            raise ValueError(f"unknown state {state!r}")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("pid is not a positive integer")
        if not isinstance(since, str):
            raise ValueError("since is not a string")
        if park_kind not in {None, "identity", "writer-startup"}:
            raise ValueError(f"unknown park kind {park_kind!r}")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason is not a string or null")
        if state == "parked":
            if park_kind is None:
                raise ValueError("parked state has no park kind")
            if not reason:
                raise ValueError("parked state has no reason")
        elif park_kind is not None or reason is not None:
            raise ValueError(f"{state} state carries park-only fields")
    except (OSError, ValueError, KeyError) as exc:
        return StateUnreadable(path=file_path, reason=str(exc))
    return DaemonState(
        state=state,
        pid=pid,
        since=since,
        park_kind=park_kind,
        reason=reason,
    )


def clear_state(store: Path) -> None:
    """Remove the derived state file after clean shutdown."""
    with suppress(FileNotFoundError):
        daemon_state_path(store).unlink()
