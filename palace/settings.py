"""The Palace-owned ``palace`` section of a composed Conric configuration.

A consumer hands Palace the composed files and the root it loaded them with. Palace
reads only its own top-level ``palace`` mapping, refuses unknown fields, and derives the
store's embedding selector through the same validator the TOML selector file uses
(:func:`palace.index.embedder_config.selector_from_mapping`). Loading, validating and
initializing make no inference request, construct no client and read no credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from conric import ConfigError, SchemaError, Value, load

from palace.index._errors import IndexError
from palace.index.embedder_config import (
    PROVIDER_ENDPOINT,
    EmbedderConfig,
    embedder_config_path,
    read_embedder_config,
    render_schema_error,
    save_embedder_config,
    selector_from_mapping,
)

__all__ = ["PalaceSettings", "initialize_store", "load_palace_settings", "palace_section"]

_SECTION = "palace"
_FIELDS = ("embedding",)
# Conric structural codes that have operator text; any other code is shown as itself.
_CODE_TEXT = {"missing_path": "required section is missing"}


@dataclass(frozen=True)
class PalaceSettings:
    """The validated ``palace`` section."""

    embedding: EmbedderConfig


def palace_section(value: Value) -> PalaceSettings:
    """Conric leaf validator for the ``palace`` mapping; refusals are ``SchemaError``."""
    if not isinstance(value, dict):
        raise SchemaError("expected_mapping")
    for key in value:
        if key not in _FIELDS:
            raise SchemaError("unknown_field", path=(key,))
    if "embedding" not in value:
        raise SchemaError("missing_field", path=("embedding",))
    try:
        embedding = selector_from_mapping(value["embedding"])
    except SchemaError as exc:
        raise SchemaError(exc.reason, path=("embedding", *exc.path)) from None
    except IndexError:
        # Conric drops the detail of a non-SchemaError failure, so the endpoint table's own
        # refusal becomes a reason token at its path.
        raise SchemaError("invalid_endpoint", path=("embedding", "endpoint")) from None
    return PalaceSettings(embedding=embedding)


def load_palace_settings(
    paths: Sequence[str | Path], *, root: str | Path | None = None
) -> PalaceSettings:
    """Load the composed files under ``root`` and validate their ``palace`` section.

    A refusal names the file, line and column Conric attributes, the dotted path and the
    reason text; no configured value is echoed.
    """
    try:
        return load(paths, root=root).validate(palace_section, path=(_SECTION,))
    except ConfigError as exc:
        location = f"{exc.source.file}:{exc.source.line}:{exc.source.column}" if exc.source else ""
        dotted = ".".join(str(part) for part in exc.path)
        text = render_schema_error(exc.reason) if exc.reason else _CODE_TEXT.get(exc.code, exc.code)
        parts = [part for part in (location, dotted, text) if part]
        raise IndexError("palace settings " + ": ".join(parts)) from None


def initialize_store(store: Path, settings: PalaceSettings) -> bool:
    """Write the store's embedding selector once; return whether it was written.

    An existing selector equal to the composed one is left alone. A differing one is
    refused and left byte-identical, because the selector is the store's recorded
    identity source.
    """
    from palace.provider_config import assert_private_boundary, require_provider

    config = settings.embedding
    require_provider("embedding", config.provider)
    if config.provider == PROVIDER_ENDPOINT:
        assert_private_boundary(store=store, watch_roots=[])
    selector = embedder_config_path(store)
    if selector.exists():
        if read_embedder_config(selector) == config:
            return False
        raise IndexError(
            f"palace settings select a different embedding than {selector}; the selector is "
            "the store's recorded identity source; move the store's chunks database and "
            "meta/embedder.toml aside, initialize again, then run 'palace index build --full'"
        )
    save_embedder_config(store, config)
    return True
