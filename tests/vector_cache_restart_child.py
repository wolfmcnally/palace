"""Subprocess child for the vector-cache restart proof (not a test module).

argv: store, watch root, marker path. Adds one note and indexes it, but stops inside
the commit, after its first feed record, writes the marker and waits to be killed.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import palace.index.vector_feed as feed
from palace.index.build import build
from tests.index.conftest import FakeEmbedder


def main() -> None:
    store, root, marker = (Path(value) for value in sys.argv[1:4])

    real_insert = feed.record_vector_insert

    def stalled(conn: sqlite3.Connection, chunk_id: str, embedding: bytes) -> None:
        real_insert(conn, chunk_id, embedding)
        marker.write_text("inside the commit", encoding="utf-8")
        time.sleep(600)

    # Writers reach the recorder through the feed module.
    setattr(feed, "record_vector_insert", stalled)  # noqa: B010
    (root / "interrupted.md").write_text("# Interrupted\n\nnever committed\n", encoding="utf-8")
    build(store=store, watch_root_filter=root, full=False, embedder=FakeEmbedder())


if __name__ == "__main__":
    main()
