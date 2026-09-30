"""The bitemporal fact surface: ``palace fact`` and the MEMORY.md model.

Implements ``policies/fact-schema.md`` — Markdown sections with YAML
frontmatter, a natural-language claim in the body, bitemporal annotations,
plain-hash provenance, and soft mutation via ``valid`` / ``superseded_by``.
"""

from __future__ import annotations

from palace.fact._errors import FactError
from palace.fact.schema import Fact, build_fact, fact_id
from palace.fact.store import MemoryDoc, parse_memory, serialize_memory

__all__ = [
    "Fact",
    "FactError",
    "MemoryDoc",
    "build_fact",
    "fact_id",
    "parse_memory",
    "serialize_memory",
]
