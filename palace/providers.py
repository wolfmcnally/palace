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
from palace.settings import initialize_store, load_palace_settings

# Refusals under these prefixes carry only paths, reason text and locations, never values,
# so they reach the operator; every other refusal is masked by the generic line.
_SHOWN_PREFIXES = (
    "endpoint ",
    "private inference ",
    "reranker ",
    "embedder selector ",
    "palace settings ",
    "bedrock ",
)


def build_subparser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "providers", help="List and explicitly configure inference providers."
    )
    commands = parser.add_subparsers(dest="provider_command", required=True)
    commands.add_parser("list", help="List the supported operation/provider kinds.")
    configure = commands.add_parser(
        "set",
        help="Validate and install one TOML selector or composed palace settings; no network.",
    )
    configure.add_argument("--operation", choices=tuple(PROVIDERS), required=True)
    configure.add_argument("--store", type=Path, required=True)
    source = configure.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="One TOML selector file; replaces the store's.")
    source.add_argument(
        "--settings",
        type=Path,
        action="append",
        help="A composed configuration file with a top-level 'palace' section; repeat to "
        "compose in order. Initializes the store's embedding selector once.",
    )


def dispatch(args: argparse.Namespace) -> int:
    if args.provider_command == "list":
        for operation, providers in PROVIDERS.items():
            print(f"{operation}: {', '.join(providers)}")
        return 0
    changed = True
    try:
        store = args.store.expanduser().resolve()
        if args.settings is not None:
            if args.operation != "embedding":
                print(
                    "error: composed palace settings select only the embedding provider",
                    file=sys.stderr,
                )
                return 1
            changed = initialize_store(store, load_palace_settings(args.settings))
        elif args.operation == "embedding":
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
        if not detail.startswith(_SHOWN_PREFIXES):
            detail = "provider configuration refused; check the documented schema and references"
        print(f"error: {detail}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "error: provider configuration could not be read or written",
            file=sys.stderr,
        )
        return 1
    if not changed:
        print("Embedding selector unchanged; no inference request was made.")
        return 0
    print(f"Configured {args.operation}; no inference request was made.")
    return 0
