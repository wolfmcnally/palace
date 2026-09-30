"""Source-code AST chunker.

Walks top-level function / class / method / interface / type-alias
declarations in a source file via `py-tree-sitter` and emits one
:class:`CodeChunk` per declaration. Languages without a bundled grammar
fall back to opaque line-window chunking via
:func:`palace.index.text.chunk_text_body`.

Bundled grammars (the lean cut shipped in Phase 2.4):

- **Python** (`.py`) — ``function_definition``, ``class_definition``
  (recursing into class bodies to surface methods as ``kind="method"``).
- **Rust** (`.rs`) — ``function_item``, ``impl_item`` (recursing into
  impl blocks to surface ``function_item`` children as
  ``kind="method"``).
- **JavaScript** (`.js`) — ``function_declaration``,
  ``class_declaration`` (recursing into class bodies to surface
  ``method_definition`` as ``kind="method"``).
- **TypeScript** (`.ts`) — JavaScript's set plus
  ``interface_declaration`` (``kind="interface"``) and
  ``type_alias_declaration`` (``kind="type"``).

To add a language:

1. Install the per-language PyPI grammar wheel (e.g. ``tree-sitter-go``).
2. Append an entry to :data:`_LANGUAGES` keyed by suffix, naming the
   grammar's ``language()`` callable, a stable language id string, and
   a per-language extractor function.
3. If the suffix is not already in
   :data:`palace.index.config.CODE_EXTENSIONS`, add it.
4. Add a fixture test to ``tests/index/test_code.py``.

(One-line follow-up: ``.tsx`` ships in this phase as a line-window
fall-back because the phase file's lean cut excludes it. Wiring it
to ``tree_sitter_typescript.language_tsx()`` is a documented two-line
add — register ``".tsx"`` in :data:`_LANGUAGES` pointing at
``tree_sitter_typescript.language_tsx()`` and a clone of
``_extract_typescript``. Left for the next phase planner.)

Failure posture:

- Unsupported suffix → line-window fall-back (no log).
- Tree-sitter parser raises → log ``palace index: code-parse-failed``
  and fall back to line-window (palace must not crash on malformed
  source).
- Parse succeeds but yields no top-level declarations (a script
  consisting only of top-level statements) → line-window fall-back.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import tree_sitter
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_typescript

from palace.index.text import chunk_text_body

__all__ = ["CodeChunk", "parse_code"]


@dataclass(frozen=True)
class CodeChunk:
    """One AST-extracted (or line-window-fallback) chunk of source code.

    For AST chunks:

    - ``symbol`` is the declaration's name
      (``node.child_by_field_name("name").text``).
    - ``kind`` is one of ``"function" | "method" | "class" | "interface"
      | "type"``.
    - ``language`` is the bundled language id
      (``"python" | "rust" | "javascript" | "typescript"``).
    - ``start_line`` / ``end_line`` are 1-based line numbers spanning
      the declaration in the source.

    For line-window fall-back chunks: ``symbol``, ``kind``, and
    ``language`` are all ``None``; ``start_line`` / ``end_line`` are
    ``0`` (not meaningful for windowed bytes).

    ``section_index`` is the document-order index of the declaration
    (or the window index, for fall-back); ``window_index`` is ``0`` for
    AST chunks and ``>= 0`` for line-window fall-back. ``body_hash`` is
    SHA-256 hex of ``body.encode("utf-8")``.
    """

    symbol: str | None
    kind: str | None
    section_index: int
    window_index: int
    body: str
    body_hash: str
    start_line: int
    end_line: int
    language: str | None


@dataclass(frozen=True)
class _RawSymbol:
    """An intermediate representation produced by each per-language extractor."""

    symbol: str
    kind: str
    start_line: int
    end_line: int
    body_bytes: bytes


_ExtractorFn = Callable[[tree_sitter.Node, bytes], Iterable[_RawSymbol]]


@dataclass(frozen=True)
class _LanguageEntry:
    """Per-language registry record: grammar + id + extractor."""

    language: tree_sitter.Language
    id: str
    extractor: _ExtractorFn


def parse_code(*, absolute_path: Path, file_bytes: bytes) -> tuple[CodeChunk, ...]:
    """Parse ``file_bytes`` as source code for ``absolute_path.suffix``.

    Returns one :class:`CodeChunk` per AST-extractable top-level
    declaration. Falls back to line-window chunking when the suffix
    has no grammar, when the parser fails, or when the file contains
    no top-level declarations.
    """
    suffix = absolute_path.suffix.lower()
    entry = _LANGUAGES.get(suffix)
    if entry is None:
        return _line_window_fallback(file_bytes)

    try:
        parser = tree_sitter.Parser(entry.language)
        tree = parser.parse(file_bytes)
    except Exception as exc:  # noqa: BLE001 — palace must not crash on bad source
        print(
            f"palace index: code-parse-failed path={absolute_path} reason={exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return _line_window_fallback(file_bytes)

    raw = list(entry.extractor(tree.root_node, file_bytes))
    if not raw:
        return _line_window_fallback(file_bytes)

    chunks: list[CodeChunk] = []
    for section_index, sym in enumerate(raw):
        body = sym.body_bytes.decode("utf-8", errors="replace")
        chunks.append(
            CodeChunk(
                symbol=sym.symbol,
                kind=sym.kind,
                section_index=section_index,
                window_index=0,
                body=body,
                body_hash=_hash_body(body),
                start_line=sym.start_line,
                end_line=sym.end_line,
                language=entry.id,
            )
        )
    return tuple(chunks)


def _line_window_fallback(file_bytes: bytes) -> tuple[CodeChunk, ...]:
    """Decode UTF-8 with replace and emit one ``CodeChunk`` per line-window."""
    body = file_bytes.decode("utf-8", errors="replace")
    if not body.strip():
        return ()
    windows = chunk_text_body(body)
    return tuple(
        CodeChunk(
            symbol=None,
            kind=None,
            section_index=0,
            window_index=window.window_index,
            body=window.body,
            body_hash=window.body_hash,
            start_line=0,
            end_line=0,
            language=None,
        )
        for window in windows
    )


# --------------------------------------------------------------------- extractors


def _name_text(node: tree_sitter.Node) -> str | None:
    """Return ``node.child_by_field_name("name").text`` decoded as UTF-8, or ``None``."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    text_bytes = name_node.text
    if text_bytes is None:
        return None
    return text_bytes.decode("utf-8", errors="replace")


def _line_span(node: tree_sitter.Node) -> tuple[int, int]:
    """Return ``(start_line, end_line)`` as 1-based line numbers."""
    return (node.start_point.row + 1, node.end_point.row + 1)


def _extract_python(root: tree_sitter.Node, source: bytes) -> Iterable[_RawSymbol]:
    for child in root.children:
        if child.type == "function_definition":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="function",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
        elif child.type == "class_definition":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="class",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for grandchild in body.children:
                if grandchild.type != "function_definition":
                    continue
                method_name = _name_text(grandchild)
                if method_name is None:
                    continue
                m_start, m_end = _line_span(grandchild)
                yield _RawSymbol(
                    symbol=method_name,
                    kind="method",
                    start_line=m_start,
                    end_line=m_end,
                    body_bytes=source[grandchild.start_byte : grandchild.end_byte],
                )


def _extract_rust(root: tree_sitter.Node, source: bytes) -> Iterable[_RawSymbol]:
    for child in root.children:
        if child.type == "function_item":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="function",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
        elif child.type == "impl_item":
            type_node = child.child_by_field_name("type")
            impl_symbol: str | None = None
            if type_node is not None and type_node.text is not None:
                impl_symbol = type_node.text.decode("utf-8", errors="replace")
            if impl_symbol is None:
                # Skip unnamed impls — defensive; tree-sitter-rust
                # always names them in practice.
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=impl_symbol,
                kind="impl",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for grandchild in body.children:
                if grandchild.type != "function_item":
                    continue
                method_name = _name_text(grandchild)
                if method_name is None:
                    continue
                m_start, m_end = _line_span(grandchild)
                yield _RawSymbol(
                    symbol=method_name,
                    kind="method",
                    start_line=m_start,
                    end_line=m_end,
                    body_bytes=source[grandchild.start_byte : grandchild.end_byte],
                )


def _extract_javascript(root: tree_sitter.Node, source: bytes) -> Iterable[_RawSymbol]:
    for child in root.children:
        if child.type == "function_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="function",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
        elif child.type == "class_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="class",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for grandchild in body.children:
                if grandchild.type != "method_definition":
                    continue
                method_name = _name_text(grandchild)
                if method_name is None:
                    continue
                m_start, m_end = _line_span(grandchild)
                yield _RawSymbol(
                    symbol=method_name,
                    kind="method",
                    start_line=m_start,
                    end_line=m_end,
                    body_bytes=source[grandchild.start_byte : grandchild.end_byte],
                )


def _extract_typescript(root: tree_sitter.Node, source: bytes) -> Iterable[_RawSymbol]:
    # JavaScript's set plus TS-specific top-level constructs.
    for child in root.children:
        if child.type == "function_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="function",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
        elif child.type == "class_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="class",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for grandchild in body.children:
                if grandchild.type != "method_definition":
                    continue
                method_name = _name_text(grandchild)
                if method_name is None:
                    continue
                m_start, m_end = _line_span(grandchild)
                yield _RawSymbol(
                    symbol=method_name,
                    kind="method",
                    start_line=m_start,
                    end_line=m_end,
                    body_bytes=source[grandchild.start_byte : grandchild.end_byte],
                )
        elif child.type == "interface_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="interface",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )
        elif child.type == "type_alias_declaration":
            symbol = _name_text(child)
            if symbol is None:
                continue
            start, end = _line_span(child)
            yield _RawSymbol(
                symbol=symbol,
                kind="type",
                start_line=start,
                end_line=end,
                body_bytes=source[child.start_byte : child.end_byte],
            )


# --------------------------------------------------------------------- registry


def _build_languages() -> dict[str, _LanguageEntry]:
    """Build the per-suffix language registry once at import time.

    The four ``tree_sitter.Language`` wrappers are constructed once and
    cached so per-file parsing does not re-wrap them on every call.
    """
    return {
        ".py": _LanguageEntry(
            language=tree_sitter.Language(tree_sitter_python.language()),
            id="python",
            extractor=_extract_python,
        ),
        ".rs": _LanguageEntry(
            language=tree_sitter.Language(tree_sitter_rust.language()),
            id="rust",
            extractor=_extract_rust,
        ),
        ".js": _LanguageEntry(
            language=tree_sitter.Language(tree_sitter_javascript.language()),
            id="javascript",
            extractor=_extract_javascript,
        ),
        ".ts": _LanguageEntry(
            language=tree_sitter.Language(tree_sitter_typescript.language_typescript()),
            id="typescript",
            extractor=_extract_typescript,
        ),
    }


_LANGUAGES: dict[str, _LanguageEntry] = _build_languages()


def _hash_body(body: str) -> str:
    """SHA-256 hex of ``body``'s UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
