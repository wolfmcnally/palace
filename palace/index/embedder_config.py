"""Store-level embedder selection and the single construction point."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import tomlkit
from conric import SchemaError, SecretRef, Value
from tomlkit import TOMLDocument

import palace.daemons.capture.config as capture_config
import palace.index.config as index_config
from palace.index._errors import IndexError
from palace.index.config import (
    BEDROCK_DIMENSIONS,
    BEDROCK_MODELS,
    REMOTE_EMBED_MAX_CONCURRENCY,
    EmbeddingIdentity,
)
from palace.index.embedder import Embedder, OllamaEmbedder, OpenRouterEmbedder
from palace.vault import resolve_vault_root

if TYPE_CHECKING:
    from palace.provider_config import EndpointConfig

__all__ = [
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENROUTER",
    "PROVIDER_ENDPOINT",
    "PROVIDER_BEDROCK",
    "EmbedPurpose",
    "EmbedderConfig",
    "assert_remote_corpus_boundary",
    "embedder_config_path",
    "identity_for",
    "load_embedder_config",
    "read_embedder_config",
    "render_schema_error",
    "resolve_embedder",
    "resolve_identity",
    "save_embedder_config",
    "selector_from_mapping",
]


PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_ENDPOINT = "endpoint"
PROVIDER_BEDROCK = "bedrock"

# What a resolved embedder is for: indexing documents or embedding a query. Only
# asymmetric providers (Bedrock's Embed v4 ``input_type``) act on it.
EmbedPurpose = Literal["document", "query"]

_PUBLISHED_FIELDS = ("published_corpus", "published_corpus_note", "published_corpus_asserted_at")
# provider -> (required fields, optional fields); one schema for every selector route.
_SELECTOR_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    PROVIDER_OLLAMA: (("provider",), ()),
    PROVIDER_OPENROUTER: (("provider", "model", "upstream", "dim"), _PUBLISHED_FIELDS),
    PROVIDER_ENDPOINT: (("provider", "model", "dim", "endpoint"), ()),
    PROVIDER_BEDROCK: (("provider", "model", "region", "dim"), ("profile", *_PUBLISHED_FIELDS)),
}
_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+\Z")
_dimensions = ", ".join(str(dim) for dim in BEDROCK_DIMENSIONS[:-1])

# The one reason-to-text table for selector refusals on every configuration route.
# Texts name fields and remedies only; no configured value is ever echoed.
_REASON_TEXT = {
    "expected_mapping": "expected a mapping",
    "unknown_field": "unknown field; remove it",
    "missing_field": "required field is missing",
    "unregistered_provider": "provider must be one of "
    + ", ".join(tuple(_SELECTOR_FIELDS)[:-1])
    + f" or {PROVIDER_BEDROCK}",
    "invalid_string": "must be a non-empty string",
    "invalid_dimension": "dim must be a positive integer",
    "unsupported_dimension": f"dim must be one of {_dimensions} or {BEDROCK_DIMENSIONS[-1]}",
    "invalid_region": "region must be an AWS Region name such as us-east-1",
    "inference_profile_model": "inference-profile model ids are refused; use the direct "
    "regional model id",
    "unsupported_model": "model must be one of " + ", ".join(sorted(BEDROCK_MODELS)),
    "invalid_boolean": "must be true or false",
    "secret_not_allowed": "an !env reference is accepted only at endpoint.credential_env",
    "invalid_endpoint": "endpoint table has missing, unknown or invalid fields; see the "
    "private-provider documentation",
}


@dataclass(frozen=True, slots=True)
class EmbedderConfig:
    """Validated contents of ``<store>/meta/embedder.toml``."""

    provider: str
    model: str
    dim: int
    upstream: str | None = None
    published_corpus: bool = False
    published_corpus_note: str | None = None
    published_corpus_asserted_at: str | None = None
    endpoint: EndpointConfig | None = None
    region: str | None = None
    profile: str | None = None


def embedder_config_path(store: Path) -> Path:
    """Return the store-level selector path."""
    return store / "meta" / "embedder.toml"


def _default_config() -> EmbedderConfig:
    return EmbedderConfig(
        provider=PROVIDER_OLLAMA,
        model=index_config.EMBEDDER_MODEL,
        dim=index_config.EMBED_DIM,
    )


def load_embedder_config(store: Path) -> EmbedderConfig:
    """Read and validate the selector; absence means local Ollama."""
    file_path = embedder_config_path(store)
    if not file_path.exists():
        return _default_config()
    return read_embedder_config(file_path)


def read_embedder_config(file_path: Path) -> EmbedderConfig:
    """Read an explicit selector file; absence is an error, not local selection."""
    try:
        document = tomlkit.parse(file_path.read_text(encoding="utf-8")).unwrap()
    except Exception:  # noqa: BLE001 — normalize parser and I/O failures
        raise IndexError(f"embedder selector {file_path} is not valid TOML") from None
    try:
        return selector_from_mapping(document)
    except SchemaError as exc:
        location = ".".join(str(part) for part in exc.path)
        prefix = f"embedder selector {file_path}: " + (f"{location}: " if location else "")
        raise IndexError(prefix + render_schema_error(exc.reason)) from None


def render_schema_error(reason: str) -> str:
    """Render a selector refusal's reason token as operator text."""
    return _REASON_TEXT.get(reason, reason)


def selector_from_mapping(value: Value) -> EmbedderConfig:
    """Validate one selector mapping, from TOML or a composed section, into a config.

    Field refusals raise :class:`conric.SchemaError` with the field's relative path and a
    reason token from the shared table. An endpoint table is validated by
    :class:`palace.provider_config.EndpointConfig`, whose own specific
    :class:`IndexError` passes through. No credential is read; a ``SecretRef`` is
    accepted only at ``endpoint.credential_env`` and normalized to its name.
    """
    if not isinstance(value, dict):
        raise SchemaError("expected_mapping")
    for key, child in value.items():
        if isinstance(child, SecretRef):
            raise SchemaError("secret_not_allowed", path=(key,))
    if "provider" not in value:
        raise SchemaError("missing_field", path=("provider",))
    provider = value["provider"]
    if not isinstance(provider, str) or not provider.strip():
        raise SchemaError("invalid_string", path=("provider",))
    if provider not in _SELECTOR_FIELDS:
        raise SchemaError("unregistered_provider", path=("provider",))
    required, optional = _SELECTOR_FIELDS[provider]
    for key in value:
        if key not in required and key not in optional:
            raise SchemaError("unknown_field", path=(key,))
    for key in required:
        if key not in value:
            raise SchemaError("missing_field", path=(key,))
    if provider == PROVIDER_OLLAMA:
        return _default_config()
    model = _string(value, "model")
    dim = value["dim"]
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise SchemaError("invalid_dimension", path=("dim",))
    if provider == PROVIDER_ENDPOINT:
        return EmbedderConfig(
            provider=provider, model=model, dim=dim, endpoint=_endpoint(value["endpoint"])
        )
    published = value.get("published_corpus", False)
    if not isinstance(published, bool):
        raise SchemaError("invalid_boolean", path=("published_corpus",))
    note = _optional_string(value, "published_corpus_note")
    asserted_at = _optional_string(value, "published_corpus_asserted_at")
    if provider == PROVIDER_OPENROUTER:
        return EmbedderConfig(
            provider=provider,
            model=model,
            dim=dim,
            upstream=_string(value, "upstream"),
            published_corpus=published,
            published_corpus_note=note,
            published_corpus_asserted_at=asserted_at,
        )
    if model not in BEDROCK_MODELS:
        profiled = model.startswith("arn:") or any(
            model.endswith("." + allowed) for allowed in BEDROCK_MODELS
        )
        raise SchemaError(
            "inference_profile_model" if profiled else "unsupported_model", path=("model",)
        )
    region = value["region"]
    if not isinstance(region, str) or not _REGION.fullmatch(region):
        raise SchemaError("invalid_region", path=("region",))
    if dim not in BEDROCK_DIMENSIONS:
        raise SchemaError("unsupported_dimension", path=("dim",))
    return EmbedderConfig(
        provider=provider,
        model=model,
        dim=dim,
        region=region,
        profile=_optional_string(value, "profile"),
        published_corpus=published,
        published_corpus_note=note,
        published_corpus_asserted_at=asserted_at,
    )


def _string(value: Mapping[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item.strip():
        raise SchemaError("invalid_string", path=(key,))
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    return _string(value, key) if key in value else None


def _endpoint(value: object) -> EndpointConfig:
    from palace.provider_config import endpoint_config

    if not isinstance(value, dict):
        raise SchemaError("expected_mapping", path=("endpoint",))
    table: dict[str, object] = {}
    for key, child in value.items():
        if isinstance(child, SecretRef):
            if key != "credential_env":
                raise SchemaError("secret_not_allowed", path=("endpoint", key))
            child = child.name
        table[key] = child
    return endpoint_config(table)


def save_embedder_config(store: Path, config: EmbedderConfig) -> None:
    """Atomically write the validated selector with an operator-facing header."""
    from palace.provider_config import require_provider, write_selector

    require_provider("embedding", config.provider)
    if not isinstance(config.model, str) or not config.model.strip():
        raise IndexError("embedder requires a non-empty model")
    if isinstance(config.dim, bool) or not isinstance(config.dim, int) or config.dim <= 0:
        raise IndexError("embedder requires a positive integer dimension")
    file_path = embedder_config_path(store)
    document: TOMLDocument = tomlkit.document()
    document.add(tomlkit.comment("palace index embedder configuration."))
    document.add(tomlkit.comment("Managed by `palace index embedder set`."))
    document.add(tomlkit.nl())
    document["provider"] = config.provider
    if config.provider == PROVIDER_ENDPOINT:
        if config.endpoint is None:
            raise IndexError("endpoint embedder requires endpoint configuration")
        document["model"] = config.model
        document["dim"] = config.dim
        document["endpoint"] = config.endpoint.document()
    if config.provider == PROVIDER_BEDROCK:
        if config.region is None:
            raise IndexError("bedrock embedder config requires a region")
        document["model"] = config.model
        document["region"] = config.region
        document["dim"] = config.dim
        if config.profile is not None:
            document["profile"] = config.profile
    if config.provider == PROVIDER_OPENROUTER:
        document["model"] = config.model
        if config.upstream is None:
            raise IndexError("openrouter embedder config requires an upstream")
        document["upstream"] = config.upstream
        document["dim"] = config.dim
    if config.provider in (PROVIDER_OPENROUTER, PROVIDER_BEDROCK):
        document.add(tomlkit.nl())
        document["published_corpus"] = config.published_corpus
        if config.published_corpus_note is not None:
            document["published_corpus_note"] = config.published_corpus_note
        if config.published_corpus_asserted_at is not None:
            document["published_corpus_asserted_at"] = config.published_corpus_asserted_at
    write_selector(file_path, document)


def identity_for(config: EmbedderConfig) -> EmbeddingIdentity:
    """Resolve the complete stamp for one validated selector."""
    provider = config.provider
    if provider == PROVIDER_ENDPOINT:
        if config.endpoint is None:
            raise IndexError("endpoint embedder requires endpoint configuration")
        provider = config.endpoint.provider_id
    if provider == PROVIDER_OPENROUTER:
        if config.upstream is None:
            raise IndexError("openrouter embedder config requires an upstream")
        provider = f"openrouter:{config.upstream}"
    if provider == PROVIDER_BEDROCK:
        if config.region is None:
            raise IndexError("bedrock embedder config requires a region")
        provider = f"bedrock:{config.region}"
    return EmbeddingIdentity(
        convention=index_config.EMBED_CONVENTION,
        model=config.model,
        provider=provider,
        dim=config.dim,
    )


def resolve_identity(store: Path) -> EmbeddingIdentity:
    """Resolve the store identity without constructing a client or using the network."""
    return identity_for(load_embedder_config(store))


def assert_remote_corpus_boundary(
    *, config: EmbedderConfig, store: Path, watch_roots: Sequence[Path]
) -> None:
    """Refuse irreversible remote egress outside the explicit published boundary."""
    if config.provider == PROVIDER_ENDPOINT:
        from palace.provider_config import assert_private_boundary

        if config.endpoint is None:
            raise IndexError("endpoint embedder requires boundary-authorized configuration")
        assert_private_boundary(store=store, watch_roots=watch_roots)
        return
    if config.provider not in (PROVIDER_OPENROUTER, PROVIDER_BEDROCK):
        return
    if (
        not config.published_corpus
        or not config.published_corpus_note
        or not config.published_corpus_note.strip()
        or not config.published_corpus_asserted_at
        or not config.published_corpus_asserted_at.strip()
    ):
        raise IndexError(
            "remote embedding requires a complete published-corpus assertion (selector fields "
            "published_corpus, published_corpus_note and published_corpus_asserted_at); for "
            "openrouter run 'palace index embedder set --provider openrouter --published-corpus "
            "--published-corpus-note <text> ...'"
        )
    resolved_store = store.expanduser().resolve()
    if resolved_store == capture_config.DEFAULT_STORE.expanduser().resolve():
        raise IndexError("remote embedding is forbidden for palace's default personal store")
    vault_root = resolve_vault_root(None).expanduser().resolve()
    for root in watch_roots:
        resolved_root = root.expanduser().resolve()
        if _is_under(resolved_root, vault_root) or _is_under(vault_root, resolved_root):
            raise IndexError(
                "remote embedding is forbidden for a watch root overlapping the "
                f"personal vault: {root}"
            )


def resolve_embedder(store: Path, *, purpose: EmbedPurpose) -> tuple[Embedder, EmbeddingIdentity]:
    """Construct the store-selected embedder for ``purpose``; never infer selection from
    credentials. Construction makes no request; symmetric providers ignore ``purpose``."""
    config = load_embedder_config(store)
    identity = identity_for(config)
    if config.provider == PROVIDER_OLLAMA:
        return OllamaEmbedder(model=config.model), identity
    if config.provider == PROVIDER_ENDPOINT:
        from palace.private_inference import EndpointEmbedder

        if config.endpoint is None:
            raise IndexError("endpoint embedder requires endpoint configuration")
        return EndpointEmbedder(
            store=store, config=config.endpoint, model=config.model, dim=config.dim
        ), identity
    if config.provider == PROVIDER_BEDROCK:
        from palace.bedrock import BedrockEmbedder

        assert config.region is not None
        return BedrockEmbedder(
            store=store,
            model=config.model,
            region=config.region,
            dim=config.dim,
            profile=config.profile,
            purpose=purpose,
        ), identity
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key is None or not api_key.strip():
        raise IndexError("remote embedder selected but OPENROUTER_API_KEY is missing or empty")
    assert config.upstream is not None
    return (
        OpenRouterEmbedder(
            store=store,
            model=config.model,
            upstream=config.upstream,
            api_key=api_key.strip(),
            dim=config.dim,
            max_concurrency=REMOTE_EMBED_MAX_CONCURRENCY,
        ),
        identity,
    )


def _is_under(child: Path, ancestor: Path) -> bool:
    try:
        child.relative_to(ancestor)
    except ValueError:
        return False
    return True
