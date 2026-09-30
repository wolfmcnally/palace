"""Hermetic, synthetic walkthrough of the production multi-store CLI.

The marked demo indexes contain fake vectors. They are never production data
and must only be queried through this driver, which installs matching stubs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import time
from collections.abc import Sequence
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from palace.cli import main as palace_main
from palace.index.build import build
from palace.index.config import EMBED_DIM
from palace.index.embedder_config import resolve_identity

MARKER = ".palace-multistore-demo.json"
SCHEMA = "palace.synthetic-multistore-demo.v1"
FILES = {
    "a": {
        "retention.md": (
            "## Retention policy\nKeep project records for seven years. "
            "Review retention policy annually.\n"
        ),
        "access.md": (
            "## Access control\nOwners approve access to records. Review permissions quarterly.\n"
        ),
        "garden.md": "## Garden\nWater the plants each morning.\n",
    },
    "b": {
        "retention.md": (
            "## Retention policy\nDelete expired backups after the retention period. "
            "Keep an audit of policy decisions.\n"
        ),
        "exceptions.md": (
            "## Policy exceptions\nA retention hold suspends ordinary deletion "
            "until the hold ends.\n"
        ),
        "kitchen.md": "## Kitchen\nLabel food with its preparation date.\n",
    },
}


class DemoEmbedder:
    """Token-hashed vectors; no learned semantic quality is claimed."""

    max_concurrency = 1

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * EMBED_DIM
            for token in re.findall(r"\w+", text.lower()):
                index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4]) % EMBED_DIM
                vector[index] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / norm for value in vector])
        return result

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


class DemoReranker:
    """Lexical overlap makes the walkthrough repeatable and free of inference."""

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        terms = set(re.findall(r"\w+", query.lower()))
        return [float(len(terms.intersection(re.findall(r"\w+", text.lower())))) for text in texts]

    def probe(self) -> None:
        pass

    def close(self) -> None:
        pass


def _inventory(base: Path) -> dict[str, str]:
    result = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("demo contains a symlink; refusing cleanup")
        if path.is_file() and path.name != MARKER:
            result[path.relative_to(base).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def _marker(base: Path) -> dict[str, object]:
    if base.is_symlink():
        raise ValueError("demo root is a symlink")
    value: dict[str, object] = json.loads((base / MARKER).read_text())
    if value.get("schema") != SCHEMA or value.get("root") != str(base):
        raise ValueError("demo marker does not match this directory")
    return value


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path.cwd().resolve()
    base = root / ".demo"
    if not (root / "bin/palace-multistore-demo").is_file():
        print("error: run the demo driver from its repository checkout", file=sys.stderr)
        return 1
    try:
        if args == ["setup"]:
            if base.exists() or base.is_symlink():
                raise ValueError(".demo already exists; nothing overwritten")
            base.mkdir()
            for name, files in FILES.items():
                source = base / f"source-{name}"
                source.mkdir()
                for filename, text in files.items():
                    (source / filename).write_text(text)
                build(
                    store=base / f"store-{name}",
                    watch_root_filter=source,
                    embedder=DemoEmbedder(),
                    log=lambda _line: None,
                )
                # Even read-only WAL connections can create mutable -wal/-shm
                # files. These disposable fixtures use rollback journals so the
                # cleanup manifest can prove exact unchanged bytes after reads.
                with closing(sqlite3.connect(base / f"store-{name}/index/chunks.sqlite")) as conn:
                    assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
            (base / MARKER).write_text(
                json.dumps(
                    {"schema": SCHEMA, "root": str(base), "files": _inventory(base)}, indent=2
                )
                + "\n"
            )
            print("Created two synthetic stores. Query through this driver; vectors are stubs.")
            return 0
        if args == ["cleanup"]:
            marker = _marker(base)
            if _inventory(base) != marker["files"]:
                raise ValueError(
                    "demo files changed; cleanup refused to preserve unexpected content"
                )
            shutil.rmtree(base)
            print("Removed the unchanged, marked demo directory.")
            return 0
        if not args or args[0] != "search":
            raise ValueError(
                "usage: palace-multistore-demo setup | search --store PATH ... QUERY | cleanup"
            )
        _marker(base)
        allowed = {
            "--store",
            "--mode",
            "--limit",
            "--json",
            "--no-rerank",
            "--verbose",
            "--allow-rerank-failure",
        }
        if any(arg.startswith("-") and arg not in allowed for arg in args):
            raise ValueError("unsupported demo option; use complete flag names and separate values")
        stores = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--store"]
        if not stores or sum(arg == "--store" for arg in args) != len(stores):
            raise ValueError("supply explicit --store PATH pairs")
        if any(not Path(store).resolve().is_relative_to(base) for store in stores):
            raise ValueError("demo search is restricted to its marked .demo directory")
        if any(arg in args for arg in ("--hyde", "--multi-query")):
            raise ValueError("query expansion is outside this fixture walkthrough")
        started = time.perf_counter()
        with ExitStack() as stack:
            stack.enter_context(patch("palace.search.OllamaEmbedder", DemoEmbedder))
            stack.enter_context(patch("palace.search.load_reranker", DemoReranker))
            stack.enter_context(patch("palace.multistore.load_reranker", DemoReranker))
            stack.enter_context(
                patch(
                    "palace.multistore.resolve_embedder",
                    lambda store: (DemoEmbedder(), resolve_identity(store)),
                )
            )
            stack.enter_context(
                patch(
                    "socket.create_connection",
                    side_effect=RuntimeError("demo must not access the network"),
                )
            )
            result = palace_main(args)
        print(
            f"synthetic demo query: {time.perf_counter() - started:.6f}s (stub providers)",
            file=sys.stderr,
        )
        return result
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
