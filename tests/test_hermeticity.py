from __future__ import annotations

import pytest

from palace.index.embedder import OllamaEmbedder


def test_outbound_embedding_request_fails_loudly(
    guard_outbound_embedding: str,
) -> None:
    assert guard_outbound_embedding == "outbound embedding guard active"
    embedder = OllamaEmbedder()
    try:
        with pytest.raises(pytest.fail.Exception, match="real outbound embedding request"):
            embedder.probe()
    finally:
        embedder.close()
