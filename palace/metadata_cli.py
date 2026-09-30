"""File-owned metadata import and indexed JSON discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from palace.daemons.capture.config import DEFAULT_STORE
from palace.index.config import chunks_db_path
from palace.metadata import (
    DocumentMetadata,
    MetadataError,
    lookup_documents,
    metadata_connection,
    parse_filters,
    replace_documents,
)


def build_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("metadata", help="Import and look up exact document metadata.")
    commands = parser.add_subparsers(dest="metadata_command", required=True)
    writer = commands.add_parser(
        "replace", help="Apply a JSON document replacement/deletion batch."
    )
    reader = commands.add_parser("lookup", help="Look up documents without inference.")
    for command in (writer, reader):
        command.add_argument("--store", type=Path, default=None)
    writer.add_argument("--input", type=Path, required=True)
    reader.add_argument("--filters", default="[]", help="JSON array of exact field predicates.")
    reader.add_argument("--limit", type=int, default=50)
    reader.add_argument("--cursor")
    reader.add_argument("--count", action="store_true", help="Also count all matching documents.")


def dispatch(args: argparse.Namespace) -> int:
    try:
        store = args.store or Path(os.environ.get("PALACE_STORE") or DEFAULT_STORE)
        database = chunks_db_path(store)
        if args.metadata_command == "replace":
            batch = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(batch, dict) or not set(batch) <= {"documents", "delete_ids"}:
                raise MetadataError("batch must contain only documents and delete_ids")
            rows = batch.get("documents", [])
            if not isinstance(rows, list):
                raise MetadataError("documents must be an array")
            documents = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not {"document_id", "fields"} <= set(row)
                    or not set(row) <= {"document_id", "fields", "watch_root", "path"}
                ):
                    raise MetadataError("invalid document record")
                documents.append(DocumentMetadata(**row))
            with metadata_connection(database, writable=True) as conn:
                generation = replace_documents(
                    conn, documents, delete_ids=batch.get("delete_ids", [])
                )
            print(json.dumps({"generation": generation}))
        else:
            filters = parse_filters(json.loads(args.filters))
            with metadata_connection(database) as conn:
                page = lookup_documents(
                    conn, filters, limit=args.limit, cursor=args.cursor, count=args.count
                )
            print(json.dumps(asdict(page)))
        return 0
    except (MetadataError, OSError, ValueError, TypeError) as exc:
        print(f"palace metadata: {exc}", file=sys.stderr)
        return 1
