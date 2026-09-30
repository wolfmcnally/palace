"""Private inference proofs with independent wire, custody and refusal oracles."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from palace.index._errors import IndexError as ProviderError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.egress import EmbeddingUsage, usage_scope
from palace.index.embedder_config import EmbedderConfig, save_embedder_config
from palace.multistore import search_stores
from palace.private_demo import ENV, NAME, TOKEN, FixtureServer
from palace.private_inference import EndpointEmbedder, EndpointReranker
from palace.provider_config import EndpointConfig, assert_private_boundary
from palace.rerank import RerankUnavailableError
from palace.rerank_config import RerankConfig, save_rerank_config
from palace.search import search, search_reranked


def _records(store: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((store / "events/cloud-egress").glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def test_private_provider_configuration_adapter_and_audit_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "private-provider-test-credential"
    text = "corpus-text-must-not-be-logged"
    monkeypatch.setenv(ENV, secret)
    store = tmp_path / "store"
    config = EndpointConfig(
        name=NAME,
        url="https://fixture.invalid/inference",
        credential_env=ENV,
        adapter="palace-json-v1",
        authorized_boundary="operator-controlled",
        boundary_note="Explicit synthetic test boundary",
        boundary_asserted_at="2026-09-06T00:00:00Z",
        max_attempts=2,
        timeout_seconds=0.1,
    )
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {}

    def valid() -> dict[str, Any]:
        return {
            "provider": NAME,
            "model": "fixture-model",
            "data": [
                {"index": 1, "embedding": [4.0, 5.0, 6.0]},
                {"index": 0, "embedding": [1.0, 2.0, 3.0]},
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == config.url
        assert request.headers["Authorization"] == f"Bearer {secret}"
        # An unavailable audit sink must prevent reaching this independent wire oracle.
        assert _records(store)[-1]["kind"].endswith("_attempt")
        if state.get("transport_error"):
            raise httpx.ConnectError(secret + text, request=request)
        return httpx.Response(
            state.get("status", 200),
            content=json.dumps(state.get("body", valid())).encode(),
            headers={"Location": "https://never-follow.invalid/"},
        )

    transport = httpx.MockTransport(handler)
    embedder = EndpointEmbedder(
        store=store, config=config, model="fixture-model", dim=3, transport=transport
    )
    try:
        assert embedder.embed([text, "second input"]) == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        first = _records(store)
        assert len(first) == 2
        assert first[0]["request_id"] == first[1]["request_id"]
        assert first[0]["operation"] == "embedding"
        assert first[1]["http_status"] == 200
        assert first[0]["input_count"] == 2
        assert first[0]["provider"] == config.provider_id
        assert first[0]["model"] == "fixture-model"
        # Phase 19: each embedding attempt counts once (not once per audit record); this provider
        # reports no cost, so a success is unknown cost; a refused attempt counts as failed.
        usage = EmbeddingUsage()
        with usage_scope(usage):
            embedder.embed([text, "second input"])
            state["status"] = 503
            with pytest.raises(ProviderError):
                embedder.embed([text, "second input"])
            state.pop("status")
        counted = usage.summary()
        assert (counted.requests, counted.failed_requests, counted.unknown_cost_requests) == (
            3,
            2,
            1,
        )
        assert counted.cost_usd is None and counted.prompt_tokens == 0
        bad = []
        value = valid()
        value["data"] = value["data"][:1]
        bad.append(value)
        value = valid()
        value["data"][0]["index"] = 0
        bad.append(value)
        value = valid()
        value["data"][0]["index"] = True
        bad.append(value)
        value = valid()
        value["data"][0]["index"] = 8
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = [1.0]
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = [True, 2, 3]
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = ["1", 2, 3]
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = [10**1000, 2, 3]
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = [float("nan"), 2, 3]
        bad.append(value)
        value = valid()
        value["data"][0]["embedding"] = [float("inf"), 2, 3]
        bad.append(value)
        value = valid()
        value["model"] = secret + text
        bad.append(value)
        value = valid()
        value["provider"] = "substitute-provider"
        bad.append(value)
        for body in bad:
            state["body"] = body
            count = len(requests)
            with pytest.raises(ProviderError) as error:
                embedder.embed([text, "second input"])
            assert len(requests) == count + 1
            assert secret not in str(error.value) and text not in str(error.value)
        state.clear()
        for status, expected_attempts in ((401, 1), (403, 1), (302, 1), (429, 2), (503, 2)):
            state.update(status=status, body={"error": secret + text})
            count = len(requests)
            with pytest.raises(ProviderError) as error:
                embedder.embed([text, "second input"])
            assert len(requests) == count + expected_attempts
            assert secret not in str(error.value) and text not in str(error.value)
        state.clear()
        state["transport_error"] = True
        with pytest.raises(ProviderError) as error:
            embedder.embed([text, "second input"])
        assert secret not in str(error.value) and text not in str(error.value)
        state.clear()
    finally:
        embedder.close()
    with pytest.raises(ProviderError, match="closed"):
        embedder.embed([text])

    reranker = EndpointReranker(
        store=store, config=config, model="fixture-model", dim=0, transport=transport
    )
    try:
        state["body"] = {
            "provider": NAME,
            "model": "fixture-model",
            "data": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
            ],
        }
        with usage_scope(usage):
            assert reranker.score("query", [text, "second input"]) == [0.1, 0.9]
        assert usage.summary().requests == 3  # reranking is not embedding spend
        state["body"]["data"][0]["index"] = 0
        with pytest.raises(ProviderError, match="indices"):
            reranker.score("query", [text, "second input"])
        state.update(status=503)
        with pytest.raises(RerankUnavailableError):
            reranker.score("query", [text])
        state.clear()
    finally:
        reranker.close()
    all_records = _records(store)
    assert {row["operation"] for row in all_records} == {"embedding", "reranking"}
    rendered = json.dumps(all_records)
    assert secret not in rendered and text not in rendered
    assert "fixture.invalid" not in rendered
    assert len({row["id"] for row in all_records}) == len(all_records)

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "events").write_text("not a directory")
    client = EndpointEmbedder(
        store=blocked, config=config, model="fixture-model", dim=3, transport=transport
    )
    count = len(requests)
    try:
        with pytest.raises(ProviderError, match="audit"):
            client.embed([text])
    finally:
        client.close()
    assert len(requests) == count
    invalid = (
        {"authorized_boundary": ""},
        {"boundary_note": ""},
        {"boundary_asserted_at": "yesterday"},
        {"url": "http://fixture.invalid"},
        {"url": "https://user:secret@fixture.invalid"},
        {"url": "https://fixture.invalid?token=secret"},
        {"adapter": "guessed-vendor"},
        {"credential_env": "literal-secret-value"},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 10**1000},
        {"max_attempts": True},
        {"max_attempts": 4},
    )
    for changes in invalid:
        with pytest.raises(ProviderError):
            replace(config, **changes)
    assert len(requests) == count
    bad_ca = tmp_path / "bad-ca.pem"
    bad_ca.write_text("not certificate material")
    with pytest.raises(ProviderError, match="CA material"):
        replace(config, ca_bundle=str(bad_ca)).runtime()
    from palace import private_inference

    real_append = private_inference.append_record

    def fail_terminal(*, store: Path, record: dict[str, Any]) -> None:
        if record["kind"].endswith("_response"):
            raise OSError(secret + text)
        real_append(store=store, record=record)

    with monkeypatch.context() as patch:
        patch.setattr(private_inference, "append_record", fail_terminal)
        state.update(status=503)
        client = EndpointEmbedder(
            store=store, config=config, model="fixture-model", dim=3, transport=transport
        )
        try:
            with pytest.raises(ProviderError, match="audit") as error:
                client.embed([text])
            assert secret not in str(error.value) and text not in str(error.value)
            assert len(requests) == count + 1  # Failed terminal audit forbids retry.
        finally:
            client.close()
            state.clear()
    monkeypatch.delenv(ENV)
    with pytest.raises(ProviderError, match="credential"):
        EndpointEmbedder(
            store=store, config=config, model="fixture-model", dim=3, transport=transport
        )
    monkeypatch.setenv(ENV, secret)
    monkeypatch.setattr("palace.provider_config.DEFAULT_STORE", store)
    with pytest.raises(ProviderError, match="personal store"):
        EndpointEmbedder(
            store=store, config=config, model="fixture-model", dim=3, transport=transport
        )
    vault = tmp_path / "personal-vault"
    monkeypatch.setattr("palace.provider_config.resolve_vault_root", lambda _arg: vault)
    with pytest.raises(ProviderError, match="personal vault"):
        assert_private_boundary(store=tmp_path / "other-store", watch_roots=[vault / "notes"])
    output = capsys.readouterr()
    assert secret not in output.out + output.err and text not in output.out + output.err


def test_private_https_build_and_search_preserve_identity_and_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    store = tmp_path / "store"
    source = tmp_path / "source"
    source.mkdir()
    phrase = "private fixture text never included in audit"
    (source / "retention.md").write_text(
        f"## Retention policy\nKeep records for seven years. {phrase}\n"
    )
    (source / "garden.md").write_text("## Garden\nWater plants every morning.\n")

    def no_substitute(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a substitute provider was constructed")

    monkeypatch.setattr("palace.search.OllamaEmbedder", no_substitute)
    monkeypatch.setattr("palace.search.load_reranker", no_substitute)
    monkeypatch.setattr("palace.multistore.load_reranker", no_substitute)
    with FixtureServer(tmp_path / "tls") as server:
        config = server.config()
        save_embedder_config(
            store, EmbedderConfig("endpoint", "fixture-embedding", EMBED_DIM, endpoint=config)
        )
        save_rerank_config(store, RerankConfig("endpoint", "fixture-reranker", config))
        assert build(store=store, watch_root_filter=source).roots
        assert {request["operation"] for request in server.requests} == {"embedding"}
        count = len(server.requests)
        answer = search_reranked(query="retention policy", store=store)
        assert answer.status == "applied" and answer.hits
        assert Path(answer.hits[0].path).name == "retention.md"
        assert answer.reranker_identity is not None
        assert answer.reranker_identity.provider == config.provider_id
        assert answer.reranker_identity.model == "fixture-reranker"
        assert [r["operation"] for r in server.requests[count:]] == ["embedding", "reranking"]
        assert search(query="retention policy", store=store)
        copy = tmp_path / "store-copy"
        shutil.copytree(store, copy)
        count = len(server.requests)
        combined = search_stores(query="retention policy", stores=[copy, store])
        assert combined.status == "applied" and len(combined.hits) == 4
        assert {hit.store for hit in combined.hits} == {store, copy}
        assert combined.reranker_identity == answer.reranker_identity
        assert [r["operation"] for r in server.requests[count:]] == ["embedding", "reranking"]

        save_rerank_config(copy, RerankConfig("endpoint", "different-model", config))
        count = len(server.requests)
        with pytest.raises(ProviderError, match="reranker identities"):
            search_stores(query="retention", stores=[store, copy])
        assert len(server.requests) == count
        save_rerank_config(copy, RerankConfig("endpoint", "fixture-reranker", config))
        vault = tmp_path / "personal-vault"
        monkeypatch.setattr("palace.provider_config.resolve_vault_root", lambda _arg: vault)
        with closing(sqlite3.connect(chunks_db_path(copy))) as conn:
            conn.execute("UPDATE chunks SET watch_root=?", (str(vault / "notes"),))
            conn.commit()
        with pytest.raises(ProviderError, match="personal vault"):
            search_stores(query="retention", stores=[store, copy])
        assert len(server.requests) == count
        with closing(sqlite3.connect(chunks_db_path(store))) as conn:
            conn.execute("UPDATE index_meta SET value='wrong-model' WHERE key='embed_model'")
            conn.commit()
        with pytest.raises(ProviderError, match="embedding-identity mismatch"):
            search_reranked(query="retention", store=store)
        assert len(server.requests) == count
        records = json.dumps(_records(store))
        assert TOKEN not in records and phrase not in records
        assert all("Authorization" not in row for row in _records(store))

    stopped = EndpointReranker(store=store, config=config, model="fixture-reranker", dim=0)
    try:
        with pytest.raises(RerankUnavailableError, match="unavailable"):
            stopped.score("query", ["synthetic passage"])
    finally:
        stopped.close()
    # Lexical retrieval isolates the stopped reranker from embedding availability.
    with pytest.raises(RerankUnavailableError):
        search_reranked(query="retention", store=store, mode="bm25", strict=None)
    degraded = search_reranked(query="retention", store=store, mode="bm25", strict=False)
    assert degraded.status == "failed" and degraded.hits
    assert degraded.reranker_identity is not None
    assert degraded.reranker_identity.provider == config.provider_id
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err and phrase not in captured.out + captured.err
