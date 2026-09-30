"""Events-log-consuming index daemon (Phase 2.4).

Tails ``<store>/events/<YYYY-MM-DD>.jsonl`` cooperatively (no IPC against
the Phase 2.2 producer), classifies each surviving change as a typed
file kind, and runs the matching per-kind pipeline end-to-end:

- **Markdown** — parse YAML frontmatter, wikilinks, hierarchical tags,
  Dataview inline fields, embeds; section-chunk by heading depth.
- **JSONL** — append-only tail; one chunk per record, advancing a
  per-path byte-offset cursor (``jsonl_cursors`` table).
- **Code** — AST-chunk via tree-sitter (Python/Rust/JavaScript/
  TypeScript starter set) or line-window fall-back for unsupported
  suffixes.
- **Opaque text** — line-window chunk; binary refusal via a
  non-printable-bytes heuristic.

Every kind re-embeds only the changed sections via a local
Ollama-backed ``qwen3-embedding:8b`` embedder, and writes the same
four-table chunks DB (``chunks`` + ``chunks_vec`` (sqlite-vec) +
``chunks_fts`` (FTS5) + ``files``) at ``<store>/index/chunks.sqlite``.
The schema-version marker stays ``"chunks-v1"``; Phase 2.4 added the
``jsonl_cursors`` table additively within the same version.

The launchd plist and the first-run full-index pass are intentionally
out of scope (Phases 2.5 and 2.6 respectively).
"""

from __future__ import annotations

from palace.index._errors import IndexError
from palace.index.build import BuildResult, build, build_root
from palace.index.code import CodeChunk, parse_code
from palace.index.config import (
    BINARY_EXTENSIONS,
    CODE_EXTENSIONS,
    EMBED_DIM,
    EMBEDDER_MODEL,
    JSONL_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    MARKDOWN_HEADING_DEPTH,
    MARKDOWN_MAX_SECTION_CHARS,
    MARKDOWN_WINDOW_CHARS,
    MARKDOWN_WINDOW_OVERLAP_CHARS,
    OLLAMA_BASE_URL,
    POLL_INTERVAL_SECONDS,
    SCHEMA_TAG,
    TEXT_EXTENSIONS,
    EmbeddingIdentity,
    FileKind,
    chunks_db_path,
    index_cursor_path,
    schema_version_path,
)
from palace.index.core import (
    FilePlan,
    IndexOutcome,
    commit_plan,
    delete_path,
    index_one,
    plan_one,
)
from palace.index.cursor import TailCursor, today_default
from palace.index.daemon_state import DaemonState, daemon_state_path
from palace.index.egress import cloud_egress_path
from palace.index.embedder import Embedder, OllamaEmbedder, OpenRouterEmbedder
from palace.index.embedder_config import (
    EmbedderConfig,
    resolve_embedder,
    resolve_identity,
)
from palace.index.jsonl import JsonlChunk, parse_jsonl_tail
from palace.index.lifecycle import (
    PLIST_FILENAME,
    PLIST_LABEL,
    PLIST_TEMPLATE_PATH,
    cli_install,
    cli_uninstall,
    install,
    uninstall,
)
from palace.index.markdown import ParsedMarkdown, Section, chunk_sections, parse_markdown
from palace.index.pipeline import classify_kind, process_change_event
from palace.index.schema import (
    ChunkRecord,
    FileRecord,
    RecordedIdentity,
    assert_identity,
    assert_identity_readable,
    compute_chunk_id,
    init_db,
    read_identity,
    stamp_identity,
)
from palace.index.server import serve
from palace.index.store_ops import StoreOpResult, copy_paths, remove_paths
from palace.index.tail import EventsTail
from palace.index.text import TextChunk, chunk_text_body, parse_text
from palace.index.update import UpdateResult, update
from palace.index.writer import WriterWorker
from palace.writer_lock import WriterLockTimeout, writer_lock, writer_lock_path

__all__ = [
    "BINARY_EXTENSIONS",
    "BuildResult",
    "CODE_EXTENSIONS",
    "ChunkRecord",
    "CodeChunk",
    "DaemonState",
    "EMBEDDER_MODEL",
    "EMBED_DIM",
    "Embedder",
    "EmbedderConfig",
    "EmbeddingIdentity",
    "EventsTail",
    "FileKind",
    "FilePlan",
    "FileRecord",
    "IndexError",
    "IndexOutcome",
    "JSONL_EXTENSIONS",
    "JsonlChunk",
    "MARKDOWN_EXTENSIONS",
    "MARKDOWN_HEADING_DEPTH",
    "MARKDOWN_MAX_SECTION_CHARS",
    "MARKDOWN_WINDOW_CHARS",
    "MARKDOWN_WINDOW_OVERLAP_CHARS",
    "OLLAMA_BASE_URL",
    "OllamaEmbedder",
    "OpenRouterEmbedder",
    "PLIST_FILENAME",
    "PLIST_LABEL",
    "PLIST_TEMPLATE_PATH",
    "POLL_INTERVAL_SECONDS",
    "ParsedMarkdown",
    "RecordedIdentity",
    "SCHEMA_TAG",
    "Section",
    "TEXT_EXTENSIONS",
    "TailCursor",
    "TextChunk",
    "StoreOpResult",
    "UpdateResult",
    "WriterLockTimeout",
    "WriterWorker",
    "assert_identity",
    "assert_identity_readable",
    "build",
    "build_root",
    "chunk_sections",
    "chunk_text_body",
    "chunks_db_path",
    "classify_kind",
    "cli_install",
    "cli_uninstall",
    "cloud_egress_path",
    "commit_plan",
    "compute_chunk_id",
    "daemon_state_path",
    "delete_path",
    "index_cursor_path",
    "index_one",
    "init_db",
    "install",
    "parse_code",
    "parse_jsonl_tail",
    "parse_markdown",
    "parse_text",
    "plan_one",
    "process_change_event",
    "read_identity",
    "resolve_embedder",
    "resolve_identity",
    "schema_version_path",
    "serve",
    "stamp_identity",
    "today_default",
    "uninstall",
    "copy_paths",
    "remove_paths",
    "update",
    "writer_lock",
    "writer_lock_path",
]
