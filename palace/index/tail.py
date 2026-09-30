"""Events-log tail reader.

The tail is a generator that yields one parsed :class:`ChangeEvent`
per line from the current cursor position, blocking via
``time.sleep(POLL_INTERVAL_SECONDS)`` between EOF checks. Day-rolls
across America/Boise midnight: when ``today`` advances past the
cursor's ``day`` and the next day's events file exists, the tail
switches to it and resets the in-memory byte offset to 0.

Malformed JSON lines log a single ``palace index: bad-line`` line to
stderr and are skipped — the events log is canonical; one bad producer
write must not stop the consumer.

The caller is responsible for checkpointing the cursor after the
per-event transaction commits; the tail itself does not auto-persist
(so a crash mid-pipeline re-runs the in-flight event on restart).
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path

from palace.daemons.capture.config import BOISE_TZ
from palace.index.config import POLL_INTERVAL_SECONDS
from palace.index.cursor import TailCursor, today_default
from palace.reindex.config import events_path
from palace.reindex.schema import ChangeEvent

__all__ = ["EventsTail"]


def _default_clock() -> date:
    """Return today's America/Boise date."""
    return datetime.now(BOISE_TZ).date()


def _default_log(line: str) -> None:
    """Default log sink: stderr, line-flushed."""
    print(line, file=sys.stderr, flush=True)


class EventsTail:
    """Resumable tail of ``<store>/events/<day>.jsonl``.

    Constructed once per :func:`palace.index.server.serve` invocation;
    its :meth:`follow` generator runs until the ``stop_event`` fires or
    the caller stops iterating.
    """

    def __init__(
        self,
        *,
        store: Path,
        cursor: TailCursor | None = None,
        clock: Callable[[], date] | None = None,
        log: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._store = store
        self._cursor = cursor if cursor is not None else today_default()
        self._clock = clock if clock is not None else _default_clock
        self._log = log if log is not None else _default_log
        self._stop_event = stop_event if stop_event is not None else threading.Event()

    # ------------------------------------------------------------------ properties

    def position(self) -> TailCursor:
        """Return the in-memory cursor (without touching disk)."""
        return self._cursor

    # ------------------------------------------------------------------ control

    def stop(self) -> None:
        """Signal :meth:`follow` to break on the next polling cycle."""
        self._stop_event.set()

    # ------------------------------------------------------------------ generator

    def follow(self) -> Iterator[tuple[ChangeEvent, int]]:
        """Yield ``(event, new_byte_offset)`` per parsed line, forever.

        ``new_byte_offset`` is the file position immediately after the
        yielded line — the caller checkpoints with this value once its
        per-event work has committed. Day-rolls are transparent: the
        consumer sees the next day's first event with offset equal to
        the byte length of that line.

        Iteration ends when ``stop_event`` is set.
        """
        while not self._stop_event.is_set():
            day = self._cursor.day
            day_path = self._store / "events" / f"{day}.jsonl"
            if not day_path.is_file():
                # Either it's a fresh day with no events yet, or the
                # producer hasn't started. Either way we sleep and
                # re-check, after probing the day-roll.
                if self._maybe_roll_day():
                    continue
                if self._wait():
                    continue
                return
            # Open + seek; read until EOF, yielding one event per line.
            with day_path.open("rb") as fh:
                fh.seek(self._cursor.byte_offset)
                while not self._stop_event.is_set():
                    line_offset = fh.tell()
                    line = fh.readline()
                    if not line:
                        # EOF — try the day-roll first; if not rolling,
                        # sleep and check again. The file handle stays
                        # open so a later append is visible on retry.
                        if self._maybe_roll_day():
                            break
                        if self._wait():
                            continue
                        return
                    if not line.endswith(b"\n"):
                        # Partial line written; rewind and retry.
                        fh.seek(line_offset)
                        if self._wait():
                            continue
                        return
                    new_offset = fh.tell()
                    event = self._parse_event(line, offset=line_offset)
                    # Even on bad lines we advance the cursor (so we
                    # don't re-read the same garbage forever).
                    self._cursor = TailCursor(
                        day=self._cursor.day,
                        byte_offset=new_offset,
                        last_event_id=(
                            event.id if event is not None else self._cursor.last_event_id
                        ),
                    )
                    if event is None:
                        continue
                    yield event, new_offset
            # Loop back to the outer while; the next iteration re-checks
            # day-roll and re-opens the (possibly new) day file.

    # ------------------------------------------------------------------ internals

    def _maybe_roll_day(self) -> bool:
        """If today's date is past ``self._cursor.day``, advance the cursor.

        Returns True if the cursor advanced.
        """
        today = self._clock()
        today_iso = today.isoformat()
        if today_iso <= self._cursor.day:
            return False
        # Today must have a real events file before we move; if the
        # producer hasn't written today's file yet we stay on the
        # previous day to keep consuming any late writes.
        candidate = events_path(self._store, today)
        if not candidate.is_file():
            return False
        self._cursor = TailCursor(
            day=today_iso,
            byte_offset=0,
            last_event_id=self._cursor.last_event_id,
        )
        return True

    def _wait(self) -> bool:
        """Sleep one poll interval. Returns True unless ``stop_event`` fired."""
        # ``Event.wait`` returns True when the event is set; we want
        # the inverse — keep iterating unless we've been asked to stop.
        return not self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS)

    def _parse_event(self, line: bytes, *, offset: int) -> ChangeEvent | None:
        """Parse one events-log line into a :class:`ChangeEvent`.

        Logs a stderr ``palace index: bad-line`` and returns ``None`` on
        malformed input — the events log is canonical, one bad line must
        not stop the consumer.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            self._log(f"palace index: bad-line offset={offset} reason=json:{exc.msg}")
            return None
        if not isinstance(obj, dict):
            self._log(f"palace index: bad-line offset={offset} reason=not-object")
            return None
        try:
            return ChangeEvent(**obj)
        except TypeError as exc:
            self._log(f"palace index: bad-line offset={offset} reason=schema:{exc}")
            return None
