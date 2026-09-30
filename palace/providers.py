"""Provider registry listing and explicit validated selector configuration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from palace.index._errors import IndexError
from palace.index.embedder_config import read_embedder_config, save_embedder_config
from palace.provider_config import PROVIDERS, assert_private_boundary, require_provider
from palace.rerank_config import read_rerank_config, save_rerank_config


def build_subparser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "providers", help="List and explicitly configure inference providers."
    )
    commands = parser.add_subparsers(dest="provider_command", required=True)
    commands.add_parser("list", help="List the supported operation/provider kinds.")
    configure = commands.add_parser(
        "set", help="Validate and install one TOML selector; no network."
    )
    configure.add_argument("--operation", choices=tuple(PROVIDERS), required=True)
    configure.add_argument("--store", type=Path, required=True)
    configure.add_argument("--config", type=Path, required=True)


def dispatch(args: argparse.Namespace) -> int:
    if args.provider_command == "list":
        for operation, providers in PROVIDERS.items():
            print(f"{operation}: {', '.join(providers)}")
        return 0
    try:
        store = args.store.expanduser().resolve()
        if args.operation == "embedding":
            embedding = read_embedder_config(args.config)
            require_provider("embedding", embedding.provider)
            if embedding.provider == "endpoint":
                assert_private_boundary(store=store, watch_roots=[])
            save_embedder_config(store, embedding)
        else:
            reranking = read_rerank_config(args.config)
            if reranking.provider == "endpoint":
                assert_private_boundary(store=store, watch_roots=[])
            save_rerank_config(store, reranking)
    except IndexError as exc:
        detail = str(exc)
        if not detail.startswith(("endpoint ", "private inference ", "reranker ")):
            detail = "provider configuration refused; check the documented schema and references"
        print(f"error: {detail}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "error: provider configuration could not be read or written",
            file=sys.stderr,
        )
        return 1
    print(f"Configured {args.operation}; no inference request was made.")
    return 0
