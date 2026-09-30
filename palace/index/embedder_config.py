"""Store-level embedder selection and the single construction point."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit
from tomlkit import TOMLDocument

import palace.daemons.capture.config as capture_config
import palace.index.config as index_config
from palace.index._errors import IndexError
from palace.index.config import (
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
    "EmbedderConfig",
    "assert_remote_corpus_boundary",
    "embedder_config_path",
    "identity_for",
    "load_embedder_config",
    "resolve_embedder",
    "resolve_identity",
    "save_embedder_config",
]


PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_ENDPOINT = "endpoint"

_OPENROUTER_KEYS = frozenset(
    {
        "provider",
        "model",
        "upstream",
        "dim",
        "published_corpus",
        "published_corpus_note",
        "published_corpus_asserted_at",
    }
)


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
        document = tomlkit.parse(file_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — normalize parser and I/O failures
        raise IndexError(f"embedder config {file_path} is not valid TOML") from None

    provider = document.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise IndexError(f"embedder config {file_path} missing non-empty 'provider'")
    if provider == PROVIDER_OLLAMA:
        unexpected = sorted(str(key) for key in document if str(key) != "provider")
        if unexpected:
            raise IndexError(
                f"embedder config {file_path}: provider=ollama does not accept "
                + ", ".join(unexpected)
            )
        return _default_config()
    if provider == PROVIDER_ENDPOINT:
        from palace.provider_config import endpoint_config

        if set(document) != {"provider", "model", "dim", "endpoint"}:
            raise IndexError("endpoint embedder requires exactly provider, model, dim and endpoint")
        model, dim = document.get("model"), document.get("dim")
        if not isinstance(model, str) or not model.strip():
            raise IndexError("endpoint embedder requires a non-empty model")
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise IndexError("endpoint embedder requires a positive integer dimension")
        return EmbedderConfig(
            provider=provider,
            model=model,
            dim=dim,
            endpoint=endpoint_config(document["endpoint"]),
        )
    if provider != PROVIDER_OPENROUTER:
        raise IndexError(f"embedder config {file_path} has unknown provider {provider!r}")

    unknown = sorted(str(key) for key in document if str(key) not in _OPENROUTER_KEYS)
    if unknown:
        raise IndexError(
            f"embedder config {file_path}: provider=openrouter does not accept "
            + ", ".join(unknown)
            + " — regenerate the selector with 'palace index embedder set "
            "--provider openrouter --model <model> --upstream <upstream> "
            "--published-corpus --published-corpus-note <text>'"
        )

    model = document.get("model")
    upstream = document.get("upstream")
    dim = document.get("dim")
    missing: list[str] = []
    if not isinstance(model, str) or not model.strip():
        missing.append("model")
    if not isinstance(upstream, str) or not upstream.strip():
        missing.append("upstream")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        missing.append("dim")
    if missing:
        raise IndexError(f"embedder config {file_path} missing or invalid: {', '.join(missing)}")
    assert isinstance(model, str)
    assert isinstance(upstream, str)
    assert isinstance(dim, int) and not isinstance(dim, bool)

    published_corpus = document.get("published_corpus", False)
    note = document.get("published_corpus_note")
    asserted_at = document.get("published_corpus_asserted_at")
    if not isinstance(published_corpus, bool):
        raise IndexError(f"embedder config {file_path}: 'published_corpus' must be boolean")
    if note is not None and not isinstance(note, str):
        raise IndexError(f"embedder config {file_path}: 'published_corpus_note' must be a string")
    if asserted_at is not None and not isinstance(asserted_at, str):
        raise IndexError(
            f"embedder config {file_path}: 'published_corpus_asserted_at' must be a string"
        )
    return EmbedderConfig(
        provider=provider,
        model=model,
        dim=dim,
        upstream=upstream,
        published_corpus=published_corpus,
        published_corpus_note=note,
        published_corpus_asserted_at=asserted_at,
    )


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
    if config.provider == PROVIDER_OPENROUTER:
        document["model"] = config.model
        if config.upstream is None:
            raise IndexError("openrouter embedder config requires an upstream")
        document["upstream"] = config.upstream
        document["dim"] = config.dim
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
    if config.provider != PROVIDER_OPENROUTER:
        return
    if (
        not config.published_corpus
        or not config.published_corpus_note
        or not config.published_corpus_note.strip()
        or not config.published_corpus_asserted_at
        or not config.published_corpus_asserted_at.strip()
    ):
        raise IndexError(
            "remote embedding requires a complete published-corpus assertion — run "
            "'palace index embedder set --provider openrouter --published-corpus "
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


def resolve_embedder(store: Path) -> tuple[Embedder, EmbeddingIdentity]:
    """Construct the store-selected embedder; never infer selection from credentials."""
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
