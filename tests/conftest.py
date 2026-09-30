"""Suite-wide safety guards for tests that can reach shared runtime state."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import palace.index.embedder as embedder_module
import palace.rerank_model as rerank_model_module


@pytest.fixture(scope="session", autouse=True)
def guard_outbound_embedding() -> Iterator[str]:
    """Fail any embedding HTTP request that lacks an explicit test transport."""
    sentinel = "outbound embedding guard active"
    original = embedder_module._build_client
    patcher = pytest.MonkeyPatch()
    patcher.delenv("OPENROUTER_API_KEY", raising=False)

    def guarded_client(*, timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
        if transport is not None:
            return original(timeout=timeout, transport=transport)

        def reject(request: httpx.Request) -> httpx.Response:
            pytest.fail(
                f"test attempted a real outbound embedding request: {request.method} {request.url}"
            )

        return original(timeout=timeout, transport=httpx.MockTransport(reject))

    patcher.setattr(embedder_module, "_build_client", guarded_client)
    try:
        yield sentinel
    finally:
        patcher.undo()


@pytest.fixture(scope="session", autouse=True)
def guard_shared_rerank_models(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Keep model state temporary and fail loudly on unstubbed downloads.

    Yields the temporary models root so a test can *assert* the invariant
    rather than only rely on it being enforced.
    """
    models_dir = tmp_path_factory.mktemp("palace-test-models")
    patcher = pytest.MonkeyPatch()
    patcher.setenv("PALACE_MODELS_DIR", str(models_dir))

    def reject_download(url: str, _destination: Path) -> None:
        pytest.fail(f"test attempted an unstubbed reranker model download: {url}")

    patcher.setattr(rerank_model_module, "_download_url", reject_download)
    try:
        yield models_dir
    finally:
        patcher.undo()
