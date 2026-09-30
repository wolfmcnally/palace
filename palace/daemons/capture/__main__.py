"""``python -m palace.daemons.capture`` entry point.

Parses ``--host``, ``--port``, ``--store`` and calls :func:`serve` directly.
This is the form launchd execs via the LaunchAgent plist at
``daemons/capture/ai.palace.capture.plist``; ``palace capture serve``
forwards to the same ``serve`` function for symmetry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from palace.daemons.capture.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_STORE
from palace.daemons.capture.server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m palace.daemons.capture")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    args = parser.parse_args(argv)
    return serve(host=args.host, port=args.port, store=args.store)


if __name__ == "__main__":
    sys.exit(main())
