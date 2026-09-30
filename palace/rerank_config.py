"""Explicit store-level reranker selection without provider substitution."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from palace.index._errors import IndexError
from palace.private_inference import EndpointReranker
from palace.provider_config import (
    EndpointConfig,
    assert_private_boundary,
    endpoint_config,
    require_provider,
    write_selector,
)
from palace.rerank import Reranker
from palace.retrieval_config import RERANK_MODEL


@dataclass(frozen=True)
class RerankIdentity:
    provider: str
    model: str


@dataclass(frozen=True)
class RerankConfig:
    provider: str = "local"
    model: str = RERANK_MODEL
    endpoint: EndpointConfig | None = None

    @property
    def identity(self) -> RerankIdentity:
        if self.provider == "endpoint":
            if self.endpoint is None:
                raise IndexError("endpoint reranking requires endpoint configuration")
            return RerankIdentity(self.endpoint.provider_id, self.model)
        require_provider("reranking", self.provider)
        return RerankIdentity(self.provider, self.model)


def read_rerank_config(path: Path) -> RerankConfig:
    try:
        value = tomlkit.parse(path.read_text())
    except (OSError, ValueError, tomlkit.exceptions.ParseError):
        raise IndexError("reranker configuration is not readable TOML") from None
    provider = value.get("provider")
    if provider == "local" and set(value) == {"provider"}:
        return RerankConfig()
    if provider != "endpoint" or set(value) != {"provider", "model", "endpoint"}:
        raise IndexError(
            "reranker requires local defaults or explicit model and endpoint configuration"
        )
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        raise IndexError("endpoint reranker requires a non-empty model")
    return RerankConfig(provider, model, endpoint_config(value["endpoint"]))


def load_rerank_config(store: Path) -> RerankConfig:
    path = store / "meta/reranker.toml"
    return read_rerank_config(path) if path.exists() else RerankConfig()


def save_rerank_config(store: Path, config: RerankConfig) -> None:
    require_provider("reranking", config.provider)
    if not isinstance(config.model, str) or not config.model.strip():
        raise IndexError("reranker requires a non-empty model")
    if config.provider == "local" and config.model != RERANK_MODEL:
        raise IndexError("local reranker configuration must preserve the current model default")
    value: dict[str, object] = {"provider": config.provider}
    if config.provider == "endpoint":
        if config.endpoint is None:
            raise IndexError("endpoint reranker requires endpoint configuration")
        value.update(model=config.model, endpoint=config.endpoint.document())
    write_selector(store / "meta/reranker.toml", value)


def preflight_reranker(config: RerankConfig, *, store: Path, watch_roots: Sequence[Path]) -> None:
    require_provider("reranking", config.provider)
    if config.provider == "endpoint":
        if config.endpoint is None:
            raise IndexError("endpoint reranker requires boundary-authorized configuration")
        assert_private_boundary(store=store, watch_roots=watch_roots)
        config.endpoint.runtime()


def construct_reranker(
    config: RerankConfig, *, store: Path, local_loader: Callable[[], Reranker]
) -> Reranker:
    require_provider("reranking", config.provider)
    if config.provider == "local":
        return local_loader()
    if config.endpoint is None:
        raise IndexError("endpoint reranker requires endpoint configuration")
    return EndpointReranker(store=store, config=config.endpoint, model=config.model, dim=0)
