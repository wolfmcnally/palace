"""``python -m palace.hooks`` convenience entry — defers to ``palace.cli``."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from palace.cli import main

    raise SystemExit(main(["hooks", *sys.argv[1:]]))
