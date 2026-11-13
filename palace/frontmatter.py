"""Load a YAML front-matter block, naming every failure to load it.

``yaml.safe_load`` reports syntax and construction failures as
``yaml.YAMLError``, but its constructors also let Python's own errors
escape for input alone. With the locked PyYAML these include
``ValueError`` (a calendar-invalid date such as ``2026-02-30``, an integer
literal past the interpreter's digit limit), ``KeyError`` and
``IndexError`` (an explicit ``!!bool``, ``!!int`` or ``!!float`` tag on an
unrecognized or empty value), ``AttributeError`` and ``TypeError`` (an
explicit ``!!timestamp`` tag on a non-timestamp or a value-key mapping),
``OverflowError`` (a long sexagesimal float) and ``RecursionError``
(nesting past the interpreter's recursion limit).

Because loading a string runs no Palace code, any exception it raises is a
property of the input (or of the loader), so :func:`load_frontmatter`
converts every one of them except ``MemoryError`` into
:class:`FrontmatterLoadError`. Callers catch that one class around this one
call and treat it as malformed front matter.
"""

from __future__ import annotations

from typing import Any

import yaml


class FrontmatterLoadError(Exception):
    """A front-matter block could not be loaded; ``__cause__`` is the loader's exception."""


def load_frontmatter(text: str) -> Any:
    """Return ``yaml.safe_load(text)``, or raise :class:`FrontmatterLoadError`.

    The error's message is the loader's message. ``MemoryError`` propagates
    unchanged; ``KeyboardInterrupt`` and ``SystemExit`` are not ``Exception``.
    """
    try:
        return yaml.safe_load(text)
    except MemoryError:
        raise
    except Exception as exc:
        raise FrontmatterLoadError(str(exc)) from exc
