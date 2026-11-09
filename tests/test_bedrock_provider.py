"""Bedrock provider and composed-settings proofs, offline through real boto3 clients.

Every AWS request is answered by a ``before-send`` stub on a real client; the suite-wide
``guard_outbound_aws`` fails any request a stub did not answer and isolates every ambient
credential, bearer-token and endpoint source. Credentials are synthetic fixtures.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import boto3
import pytest
from botocore.credentials import RefreshableCredentials

import palace.bedrock as bedrock
from palace.bedrock import BedrockEmbedder
from palace.cli import main as palace_main
from palace.index._errors import IndexError as PalaceError
from palace.index.config import EMBED_CONVENTION
from palace.index.egress import EmbeddingUsage, usage_scope
from palace.index.embedder_config import (
    EmbedderConfig,
    EmbedPurpose,
    identity_for,
    read_embedder_config,
    resolve_embedder,
)
from palace.settings import PalaceSettings, initialize_store, load_palace_settings
from tests.conftest import aws_env_credential_names

REGION = "us-east-1"
INHERITED_TOKEN = "SYNTHETICINHERITEDTOKEN"
INHERITED_ACCOUNT = "999999999999"
MODEL = "cohere.embed-v4:0"
DIM = 1536
CONTROL_HOST = "https://bedrock.us-east-1.amazonaws.com"
RUNTIME_HOST = "https://bedrock-runtime.us-east-1.amazonaws.com"
DIMENSION_TEXT = "dim must be one of 256, 512, 1024 or 1536"
MISSING_TEXT = "required field is missing"
PROFILE_TEXT = "inference-profile model ids are refused; use the direct regional model id"
UNKNOWN_TEXT = "unknown field; remove it"


# --------------------------------------------------------------------- stubs


def vector_for(text: str, dim: int = DIM) -> list[float]:
    """A deterministic vector from a hash of the text's last line."""
    digest = hashlib.sha256(text.strip().splitlines()[-1].encode("utf-8")).digest()
    unit = [((value % 2000) - 1000) / 1000.0 for value in struct.unpack("<16H", digest)]
    return (unit * (dim // len(unit) + 1))[:dim]


def _response(
    request: Any, status: int, body: object, headers: dict[str, str] | None = None
) -> Any:
    from botocore.awsrequest import AWSResponse
    from urllib3.response import HTTPResponse

    data = json.dumps(body).encode()
    merged = {"content-type": "application/json", "content-length": str(len(data))}
    merged.update(headers or {})
    raw = HTTPResponse(body=io.BytesIO(data), headers=merged, status=status, preload_content=False)
    return AWSResponse(request.url, status, merged, raw)


class BedrockStub:
    """Answers both Bedrock clients and records every request they would have sent."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.mode = "none"
        self.errors: list[tuple[int, str]] = []
        self.shape = "list"
        self.body: Callable[[list[str], int], object] | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = bedrock.open_bedrock_client

        def opener(service: str, *, profile: str | None, region: str) -> Any:
            client = real(service, profile=profile, region=region)
            client.meta.events.register("before-send", self.answer)
            return client

        monkeypatch.setattr(bedrock, "open_bedrock_client", opener)

    def answer(self, request: Any, **_: Any) -> Any:
        self.requests.append(request)
        if urlsplit(request.url).path == "/data-retention":
            return _response(request, 200, {"mode": self.mode})
        if self.errors:
            status, code = self.errors.pop(0)
            return _response(request, status, {"message": "fixture"}, {"x-amzn-ErrorType": code})
        payload = json.loads(request.body)
        texts, dim = payload["texts"], payload["output_dimension"]
        if self.body is not None:
            return _response(request, 200, self.body(texts, dim))
        vectors = [vector_for(text, dim) for text in texts]
        if self.shape == "list":
            return _response(
                request, 200, {"embeddings": vectors, "response_type": "embeddings_floats"}
            )
        return _response(
            request, 200, {"embeddings": {"float": vectors}, "response_type": "embeddings_by_type"}
        )

    def invokes(self) -> list[Any]:
        return [r for r in self.requests if urlsplit(r.url).path != "/data-retention"]

    def retention_reads(self) -> list[Any]:
        return [r for r in self.requests if urlsplit(r.url).path == "/data-retention"]


def header(request: Any, name: str) -> str:
    value = request.headers[name]
    return value.decode() if isinstance(value, bytes) else str(value)


def audit(store: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((store / "events" / "cloud-egress").glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def fixture_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic keys and nothing else the environment provider would read."""
    for name in aws_env_credential_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture")


def embedder(store: Path, purpose: EmbedPurpose = "document") -> BedrockEmbedder:
    return BedrockEmbedder(
        store=store,
        model=MODEL,
        region=REGION,
        dim=DIM,
        profile=None,
        purpose=purpose,
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


DEFAULTS = """palace:
  embedding:
    provider: bedrock
    model: cohere.embed-v4:0
    region: us-east-1
"""
SITE = """palace:
  embedding:
    dim: 1536
"""


# --------------------------------------------------------------------- F1


def test_bedrock_settings_and_selector_refuse_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_client(*_: object, **__: object) -> None:
        pytest.fail("configuration constructed an AWS client")

    monkeypatch.setattr(bedrock, "open_bedrock_client", no_client)
    defaults = write(tmp_path / "defaults.yaml", DEFAULTS)
    site = write(tmp_path / "site.yaml", SITE)

    # A two-file composition parses to the Bedrock selector and its Region-qualified identity.
    settings = load_palace_settings([defaults, site], root=tmp_path)
    expected = EmbedderConfig(provider="bedrock", model=MODEL, dim=DIM, region=REGION)
    _assert_representation_selector(tmp_path / "representation", expected)
    assert settings == PalaceSettings(embedding=expected)
    identity = identity_for(settings.embedding)
    assert (identity.provider, identity.model, identity.dim, identity.convention) == (
        "bedrock:us-east-1",
        MODEL,
        DIM,
        EMBED_CONVENTION,
    )

    def composed_refusal(defaults_text: str, site_text: str) -> str:
        a = write(tmp_path / "case" / "defaults.yaml", defaults_text)
        b = write(tmp_path / "case" / "site.yaml", site_text)
        with pytest.raises(PalaceError) as refused:
            load_palace_settings([a, b], root=tmp_path / "case")
        return str(refused.value)

    def toml_refusal(text: str) -> str:
        selector = write(tmp_path / "case" / "embedder.toml", text)
        with pytest.raises(PalaceError) as refused:
            read_embedder_config(selector)
        return str(refused.value)

    # Unknown fields name their path and the source file, line and value column.
    site_file = (tmp_path / "case" / "site.yaml").resolve()
    message = composed_refusal(DEFAULTS, SITE + "  extra: 1\n")
    assert message == f"palace settings {site_file}:4:10: palace.extra: {UNKNOWN_TEXT}"
    message = composed_refusal(DEFAULTS, SITE + "    extra: 1\n")
    assert message == f"palace settings {site_file}:4:12: palace.embedding.extra: {UNKNOWN_TEXT}"

    # Missing Region, an inference-profile id and an unsupported dimension refuse on both
    # routes with the same text.
    toml = 'provider = "bedrock"\nmodel = "cohere.embed-v4:0"\nregion = "us-east-1"\ndim = 1536\n'
    selector_file = tmp_path / "case" / "embedder.toml"
    defaults_file = (tmp_path / "case" / "defaults.yaml").resolve()
    cases = (
        (
            DEFAULTS.replace("    region: us-east-1\n", ""),
            SITE,
            toml.replace('region = "us-east-1"\n', ""),
            "region",
            MISSING_TEXT,
            site_file,  # the nearest winning node: the last file defining palace.embedding
        ),
        (
            DEFAULTS.replace(MODEL, "us." + MODEL),
            SITE,
            toml.replace(MODEL, "us." + MODEL),
            "model",
            PROFILE_TEXT,
            defaults_file,
        ),
        (
            DEFAULTS,
            SITE.replace("1536", "1000"),
            toml.replace("1536", "1000"),
            "dim",
            DIMENSION_TEXT,
            site_file,
        ),
    )
    for defaults_text, site_text, toml_text, field, text, source in cases:
        composed = composed_refusal(defaults_text, site_text)
        assert composed.startswith(f"palace settings {source}:")
        assert composed.endswith(f": palace.embedding.{field}: {text}")
        assert toml_refusal(toml_text) == f"embedder selector {selector_file}: {field}: {text}"
    assert "1000" not in composed_refusal(DEFAULTS, SITE.replace("1536", "1000"))

    # A secret reference is accepted only where a credential reference belongs.
    endpoint = """palace:
  embedding:
    provider: endpoint
    model: fixture-model
    dim: 8
    endpoint:
      name: fixture
      url: https://fixture.invalid/inference
      credential_env: !env FIXTURE_CREDENTIAL
      adapter: palace-json-v1
      authorized_boundary: operator-controlled
      boundary_note: Synthetic boundary
      boundary_asserted_at: "2026-11-07T00:00:00Z"
"""
    referenced = write(tmp_path / "endpoint.yaml", endpoint)
    selected = load_palace_settings([referenced], root=tmp_path).embedding
    assert selected.endpoint is not None
    assert selected.endpoint.credential_env == "FIXTURE_CREDENTIAL"
    message = composed_refusal(DEFAULTS, SITE + "    profile: !env FIXTURE_PROFILE\n")
    assert message.endswith(
        ": palace.embedding.profile: an !env reference is accepted only at endpoint.credential_env"
    )

    # Initialization writes once, leaves an equal selector alone and refuses a different one.
    store = tmp_path / "store"
    selector = store / "meta" / "embedder.toml"
    assert initialize_store(store, settings) is True
    written = selector.read_bytes()
    assert read_embedder_config(selector) == expected
    assert initialize_store(store, settings) is False
    assert selector.read_bytes() == written
    other = PalaceSettings(embedding=EmbedderConfig("bedrock", MODEL, 1024, region=REGION))
    with pytest.raises(PalaceError, match="select a different embedding"):
        initialize_store(store, other)
    assert selector.read_bytes() == written

    # The command line: configured, unchanged, inspectable, and readable refusals.
    cli_store = tmp_path / "cli-store"
    argv = ["providers", "set", "--operation", "embedding", "--store", str(cli_store)]
    composed_argv = argv + ["--settings", str(defaults), "--settings", str(site)]
    capsys.readouterr()
    assert palace_main(composed_argv) == 0
    assert capsys.readouterr().out == "Configured embedding; no inference request was made.\n"
    assert palace_main(composed_argv) == 0
    assert (
        capsys.readouterr().out == "Embedding selector unchanged; no inference request was made.\n"
    )
    assert palace_main(["index", "embedder", "show", "--store", str(cli_store)]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert (
        "provider: bedrock" in shown and "region: us-east-1" in shown and "profile: none" in shown
    )
    assert "published_corpus: false" in shown
    assert (
        f"stamped_identity: convention={EMBED_CONVENTION} model={MODEL} "
        "provider=bedrock:us-east-1 dim=1536"
    ) in shown
    narrow = write(tmp_path / "narrow.yaml", SITE.replace("1536", "1000"))
    assert palace_main(argv + ["--settings", str(defaults), "--settings", str(narrow)]) == 1
    assert DIMENSION_TEXT in capsys.readouterr().err
    rerank = ["providers", "set", "--operation", "reranking", "--store", str(cli_store)]
    assert palace_main(rerank + ["--settings", str(defaults)]) == 1
    assert "select only the embedding provider" in capsys.readouterr().err

    # Resolution constructs the provider without a client; nothing was sent or audited.
    resolved, resolved_identity = resolve_embedder(cli_store, purpose="document")
    assert isinstance(resolved, BedrockEmbedder) and resolved_identity == identity
    resolved.close()
    for root in (store, cli_store):
        assert not (root / "events" / "cloud-egress").exists()


# --------------------------------------------------------------------- F2


def test_bedrock_embedder_checks_retention_then_sends_bounded_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_outbound_aws: Path
) -> None:
    store = tmp_path / "store"
    stub = BedrockStub()
    stub.install(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(bedrock, "_sleep", sleeps.append)

    # Credential witness: the guard's isolation holds, so nothing resolves and probe refuses.
    assert os.environ["AWS_CONFIG_FILE"] == str(guard_outbound_aws / "config")
    assert os.environ["BOTO_CONFIG"] == str(guard_outbound_aws / "boto")
    assert not any(
        os.environ.get(name)
        for name in (
            *aws_env_credential_names(),
            "AWS_PROFILE",
            "AWS_CREDENTIAL_FILE",
            "AWS_BEARER_TOKEN_BEDROCK",
        )
    )
    assert {"AWS_SECURITY_TOKEN", "AWS_CREDENTIAL_EXPIRATION", "AWS_ACCOUNT_ID"} <= set(
        aws_env_credential_names()
    )
    assert boto3.Session(region_name=REGION).get_credentials() is None
    with pytest.raises(PalaceError, match="bedrock credentials unavailable"):
        embedder(store).probe()
    assert stub.requests == [] and audit(store) == []

    # Positive control: each legacy credential file the guard redirects really is consulted.
    ec2_file = write(tmp_path / "decoy-ec2", "AWSAccessKeyId=DECOYEC2MARKER\nAWSSecretKey=decoy\n")
    monkeypatch.setenv("AWS_CREDENTIAL_FILE", str(ec2_file))
    assert boto3.Session(region_name=REGION).get_credentials().access_key == "DECOYEC2MARKER"
    monkeypatch.delenv("AWS_CREDENTIAL_FILE")
    boto_file = write(
        tmp_path / "decoy-boto",
        "[Credentials]\naws_access_key_id = DECOYBOTOMARKER\naws_secret_access_key = decoy\n",
    )
    monkeypatch.setenv("BOTO_CONFIG", str(boto_file))
    assert boto3.Session(region_name=REGION).get_credentials().access_key == "DECOYBOTOMARKER"
    monkeypatch.setenv("BOTO_CONFIG", str(guard_outbound_aws / "boto"))
    assert boto3.Session(region_name=REGION).get_credentials() is None

    # Positive control: inherited token, expiry and account values would ride along with
    # any environment keys; the fixture credentials carry none of them.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", INHERITED_TOKEN)
    monkeypatch.setenv("AWS_CREDENTIAL_EXPIRATION", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("AWS_ACCOUNT_ID", INHERITED_ACCOUNT)
    inherited = boto3.Session(region_name=REGION).get_credentials()
    assert isinstance(inherited, RefreshableCredentials)
    frozen = inherited.get_frozen_credentials()
    assert (frozen.token, frozen.account_id) == (INHERITED_TOKEN, INHERITED_ACCOUNT)
    fixture_credentials(monkeypatch)
    clean = boto3.Session(region_name=REGION).get_credentials()
    assert not isinstance(clean, RefreshableCredentials)
    frozen = clean.get_frozen_credentials()
    assert (frozen.access_key, frozen.token, frozen.account_id) == ("fixture", None, None)

    # Retention fails closed: every mode but none refuses after one audited read, no invoke.
    for mode in ("default", "aws_review", "provider_data_share", "inherit"):
        stub.mode = mode
        before_requests, before_audit = len(stub.requests), len(audit(store))
        with pytest.raises(PalaceError, match=f"retention mode in us-east-1 is '{mode}'"):
            embedder(store).embed(["fixture text"])
        new = stub.requests[before_requests:]
        assert [urlsplit(r.url).path for r in new] == ["/data-retention"]
        records = audit(store)[before_audit:]
        assert [
            (r["kind"], r["operation"], r["attempt"], r["status"], r["region"]) for r in records
        ] == [("bedrock_retention_check", "GetAccountDataRetention", 1, "ok", REGION)]
    assert stub.invokes() == []
    stub.mode = "none"

    # Endpoint witness: every ambient override is set, yet both clients stay on the Region's
    # standard endpoints; a client opened without the flag honors the override (control).
    profile_config = write(
        tmp_path / "aws-config", "[default]\nendpoint_url = https://profile.override.invalid\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(profile_config))
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://global.override.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK", "https://control.override.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "https://runtime.override.invalid")
    control = bedrock.open_bedrock_client("bedrock", profile=None, region=REGION)
    runtime = bedrock.open_bedrock_client("bedrock-runtime", profile=None, region=REGION)
    assert (control.meta.endpoint_url, runtime.meta.endpoint_url) == (CONTROL_HOST, RUNTIME_HOST)
    control.close()
    runtime.close()
    honoring = boto3.Session(region_name=REGION).client("bedrock-runtime")
    assert honoring.meta.endpoint_url == "https://runtime.override.invalid"
    honoring.close()

    # Requests and bounds: 200 documents are three batches of 96, 96 and 8 with exact bodies.
    documents = [f"document number {index}" for index in range(200)]
    first = embedder(store)
    usage = EmbeddingUsage()
    before = len(stub.requests)
    with usage_scope(usage):
        vectors = first.embed(documents)
    sent = stub.requests[before:]
    assert [urlsplit(r.url).path for r in sent][0] == "/data-retention"
    assert all(r.url.startswith(CONTROL_HOST) for r in sent[:1])
    invokes = sent[1:]
    assert [len(json.loads(r.body)["texts"]) for r in invokes] == [96, 96, 8]
    for index, request in enumerate(invokes):
        assert request.url.startswith(RUNTIME_HOST)
        assert unquote(urlsplit(request.url).path) == "/model/cohere.embed-v4:0/invoke"
        assert header(request, "Content-Type") == header(request, "Accept") == "application/json"
        authorization = header(request, "Authorization")
        assert authorization.startswith("AWS4-HMAC-SHA256 Credential=fixture/")
        assert "X-Amz-Security-Token" not in request.headers
        assert not any(
            marker in str(request.headers) for marker in (INHERITED_TOKEN, INHERITED_ACCOUNT)
        )
        assert f"/{REGION}/bedrock/aws4_request" in authorization
        assert json.loads(request.body) == {
            "texts": documents[index * 96 : index * 96 + 96],
            "input_type": "search_document",
            "embedding_types": ["float"],
            "truncate": "NONE",
            "output_dimension": DIM,
        }
    assert vectors == [vector_for(text) for text in documents]
    records = [r for r in audit(store) if r["kind"] == "cloud_embed_call"]
    assert [
        (
            r["provider"],
            r["upstream_configured"],
            r["input_count"],
            r["status"],
            r["input_type"],
            r["http_status"],
        )
        for r in records
    ] == [
        ("bedrock:us-east-1", REGION, count, "ok", "search_document", 200) for count in (96, 96, 8)
    ]
    assert all("document number" not in json.dumps(r) for r in audit(store))
    assert (usage.summary().requests, usage.summary().failed_requests) == (3, 0)

    # The retention check runs once per instance; a query carries search_query.
    before = len(stub.requests)
    first.embed(["again"])
    assert [urlsplit(r.url).path for r in stub.requests[before:]] == [
        "/model/cohere.embed-v4%3A0/invoke"
    ]
    first.close()
    query = embedder(store, purpose="query")
    query.embed(["what is the fixture"])
    assert json.loads(stub.invokes()[-1].body)["input_type"] == "search_query"

    # Budget splitting below 96 inputs; one input above the budget refuses with no send.
    large = ["a" * 50_000 + f" {index}" for index in range(5)]
    before = len(stub.invokes())
    query.embed(large)
    assert [len(json.loads(r.body)["texts"]) for r in stub.invokes()[before:]] == [2, 2, 1]
    before = len(stub.requests)
    with pytest.raises(PalaceError, match="exceeds the conservative request budget"):
        embedder(store).embed(["a" * 120_000])
    assert len(stub.requests) == before

    # Both documented response shapes are accepted; anything else refuses.
    stub.shape = "by-type"
    assert query.embed(["by type"]) == [vector_for("by type")]
    stub.shape = "list"
    malformed: tuple[tuple[Callable[[list[str], int], object], str], ...] = (
        (lambda texts, dim: {"embeddings": "fixture"}, "no float embeddings array"),
        (lambda texts, dim: {"embeddings": [[0.0] * dim]}, "returned 1 vectors for 2 inputs"),
        (lambda texts, dim: {"embeddings": [[0.0] * 8 for _ in texts]}, "has dim 8; expected 1536"),
        (lambda texts, dim: {"embeddings": [["x"] * dim for _ in texts]}, "contains a non-number"),
    )
    for body, message in malformed:
        stub.body = body
        with pytest.raises(PalaceError, match=message):
            query.embed(["one", "two"])
    stub.body = None

    # Retries: throttling then success is two audited sends with one recorded backoff; a
    # validation error is not retried.
    stub.errors = [(429, "ThrottlingException")]
    before_sends, before_audit = len(stub.invokes()), len(audit(store))
    assert query.embed(["retried"]) == [vector_for("retried")]
    attempts = audit(store)[before_audit:]
    assert len(stub.invokes()) - before_sends == len(attempts) == 2
    assert [(r["attempt"], r["status"]) for r in attempts] == [
        (1, "ThrottlingException"),
        (2, "ok"),
    ]
    assert sleeps == [1.0]
    stub.errors = [(400, "ValidationException")]
    before_sends = len(stub.invokes())
    with pytest.raises(PalaceError, match="InvokeModel failed in us-east-1: ValidationException"):
        query.embed(["invalid"])
    assert len(stub.invokes()) - before_sends == 1 and sleeps == [1.0]
    query.close()

    # Guard witness: an unstubbed real request fails the test instead of leaving the machine.
    monkeypatch.undo()
    fixture_credentials(monkeypatch)
    unstubbed = bedrock.open_bedrock_client("bedrock-runtime", profile=None, region=REGION)
    with pytest.raises(pytest.fail.Exception, match="real outbound AWS request"):
        unstubbed.invoke_model(modelId=MODEL, body=b"{}")
    unstubbed.close()


def _assert_representation_selector(base: Path, ordinary: EmbedderConfig) -> None:
    from dataclasses import asdict, replace

    from conric import SchemaError

    from palace.index import EmbeddingRepresentation
    from palace.index.embedder_config import (
        load_embedder_config,
        save_embedder_config,
        selector_from_mapping,
    )
    from palace.index.representation import parse_representation

    descriptor = EmbeddingRepresentation("literal-v1", "fixture-v1", "md-v1", "heading-v1")
    selected = replace(ordinary, representation=descriptor)
    save_embedder_config(base, selected)
    assert load_embedder_config(base) == selected
    assert parse_representation(identity_for(selected).convention) == descriptor
    assert identity_for(ordinary).convention == EMBED_CONVENTION
    config_file = write(
        base / "composed.yaml",
        "palace:\n  embedding:\n    provider: ollama\n    representation:\n"
        "      convention: literal-v1\n      implementation: fixture-v1\n"
        "      chunker: md-v1\n      name_policy: heading-v1\n",
    )
    assert load_palace_settings([config_file], root=base).embedding.representation == descriptor
    for wrong in (
        {},
        {**asdict(descriptor), "extra": "x"},
        {**asdict(descriptor), "chunker": ""},
        {**asdict(descriptor), "implementation": "x" * 257},
        {**asdict(descriptor), "name_policy": False},
    ):
        with pytest.raises(SchemaError, match="invalid_representation"):
            selector_from_mapping({"provider": "ollama", "representation": wrong})
    with pytest.raises(PalaceError, match="descriptor"):
        parse_representation(identity_for(selected).convention + " ")
