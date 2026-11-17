"""Bedrock provider and composed-settings proofs, offline through real boto3 clients.

Every AWS request is answered by a ``before-send`` stub on a real client; the suite-wide
``guard_outbound_aws`` fails any request a stub did not answer and isolates every ambient
credential, bearer-token and endpoint source. Credentials are synthetic fixtures.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import struct
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import boto3
import pytest
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ReadTimeoutError

import palace.bedrock as bedrock
from palace.bedrock import BedrockEmbedder
from palace.cli import main as palace_main
from palace.index._errors import IndexError as PalaceError
from palace.index._errors import ProviderCallError, ProviderThrottledError
from palace.index.config import EMBED_CONVENTION
from palace.index.egress import EmbeddingUsage, usage_scope
from palace.index.embedder import request_units
from palace.index.embedder_config import (
    EmbedderConfig,
    EmbedPurpose,
    identity_for,
    read_embedder_config,
    resolve_embedder,
)
from palace.index.pacing import PacedRequest, PacerRefused, call_scope
from palace.settings import PalaceSettings, initialize_store, load_palace_settings
from tests.conftest import aws_env_credential_names

REGION = "us-east-1"
INHERITED_TOKEN = "SYNTHETICINHERITEDTOKEN"
INHERITED_ACCOUNT = "999999999999"
BEARER_TOKEN = "SYNTHETICBEDROCKAPIKEYFIXTURE"
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
        # An invoke answer to inject: (status, code), (status, code, extra headers), or a
        # transport exception to raise in place of sending.
        # ``None`` answers that invoke normally.
        self.errors: list[tuple[int, str] | tuple[int, str, dict[str, str]] | Exception | None] = []
        self.hook: Callable[[Any], None] | None = None  # runs on each invoke before answering
        self.retention_errors: list[tuple[int, str]] = []
        self.error_message = "fixture"
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
            if self.retention_errors:
                status, code = self.retention_errors.pop(0)
                return _response(
                    request, status, {"message": self.error_message}, {"x-amzn-ErrorType": code}
                )
            return _response(request, 200, {"mode": self.mode})
        if self.hook is not None:
            self.hook(request)
        error = self.errors.pop(0) if self.errors else None
        if isinstance(error, Exception):
            raise error
        if error is not None:
            status, code, *extra = error
            headers = {"x-amzn-ErrorType": code, **(extra[0] if extra else {})}
            return _response(request, status, {"message": self.error_message}, headers)
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
    with pytest.raises(
        PalaceError, match="bedrock credentials unavailable.*a Bedrock API key in AWS_BEARER_TOKEN"
    ):
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

    # Retries: throttling then success is two audited sends with one recorded backoff, the
    # first exponential step jittered downward by at most half; a validation error is not
    # retried.
    stub.errors = [(429, "ThrottlingException")]
    before_sends, before_audit = len(stub.invokes()), len(audit(store))
    assert query.embed(["retried"]) == [vector_for("retried")]
    attempts = audit(store)[before_audit:]
    assert len(stub.invokes()) - before_sends == len(attempts) == 2
    assert [(r["attempt"], r["status"]) for r in attempts] == [
        (1, "ThrottlingException"),
        (2, "ok"),
    ]
    assert len(sleeps) == 1 and 0.5 <= sleeps[0] <= 1.0
    stub.errors = [(400, "ValidationException")]
    before_sends = len(stub.invokes())
    with pytest.raises(PalaceError, match="InvokeModel failed in us-east-1: ValidationException"):
        query.embed(["invalid"])
    assert len(stub.invokes()) - before_sends == 1 and len(sleeps) == 1
    query.close()

    # Bearer admission: a Bedrock API key alone is admitted, and the real SDK signs both the
    # retention read and InvokeModel with it. Admission is not authorization: retention
    # still decides, and the token never reaches an error or the audit.
    for name in aws_env_credential_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", BEARER_TOKEN)
    assert boto3.Session(region_name=REGION).get_credentials() is None
    bearer_store = tmp_path / "bearer-store"
    before = len(stub.requests)
    token_only = embedder(bearer_store)
    assert token_only.embed(["bearer text"]) == [vector_for("bearer text")]
    signed = stub.requests[before:]
    assert [(r.url.split("/")[2], urlsplit(r.url).path) for r in signed] == [
        ("bedrock.us-east-1.amazonaws.com", "/data-retention"),
        ("bedrock-runtime.us-east-1.amazonaws.com", "/model/cohere.embed-v4%3A0/invoke"),
    ]
    assert [header(r, "Authorization") for r in signed] == [f"Bearer {BEARER_TOKEN}"] * 2
    token_only.close()
    # A service message is never echoed: a token inside one reaches neither the refusal nor
    # the audit.
    stub.retention_errors = [(403, "AccessDeniedException")]
    stub.error_message = f"key {BEARER_TOKEN} may not read retention"
    before = len(stub.requests)
    with pytest.raises(
        PalaceError, match="GetAccountDataRetention failed in us-east-1: AccessDeniedException"
    ) as denied:
        embedder(bearer_store).embed(["denied"])
    assert str(denied.value) == (
        "bedrock GetAccountDataRetention failed in us-east-1: AccessDeniedException"
    )
    stub.error_message = "fixture"
    for mode in ("default", "provider_data_share"):
        stub.mode = mode
        with pytest.raises(PalaceError, match=f"retention mode in us-east-1 is '{mode}'"):
            embedder(bearer_store).embed(["shared"])
    stub.mode = "none"
    assert [urlsplit(r.url).path for r in stub.requests[before:]] == ["/data-retention"] * 3
    assert [r["status"] for r in audit(bearer_store)][-3:] == ["AccessDeniedException", "ok", "ok"]

    # Keys and a token together: the SDK prefers the token (its own precedence, documented).
    fixture_credentials(monkeypatch)
    before = len(stub.requests)
    both = embedder(bearer_store)
    both.embed(["both"])
    assert [header(r, "Authorization") for r in stub.requests[before:]] == [
        f"Bearer {BEARER_TOKEN}"
    ] * 2
    both.close()
    assert BEARER_TOKEN not in json.dumps(audit(bearer_store))

    # A set-but-blank token refuses before any request even when keys resolve; the control
    # shows an ordinary client would otherwise sign with the blank token.
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", " ")
    blank: list[str] = []

    def record_blank(request: Any, **_: Any) -> Any:
        blank.append(header(request, "Authorization"))
        return _response(request, 200, {"mode": "none"})

    control_client = boto3.Session(region_name=REGION).client("bedrock")
    control_client.meta.events.register("before-send", record_blank)
    control_client.get_account_data_retention()
    control_client.close()
    assert blank == ["Bearer  "]
    before = len(stub.requests)
    with pytest.raises(PalaceError, match="AWS_BEARER_TOKEN_BEDROCK is set but blank"):
        embedder(bearer_store).probe()
    assert len(stub.requests) == before

    # A token outside the bearer grammar (here newline-terminated) refuses before any request
    # without echoing it; the control shows the HTTP library would echo it in its error.
    newline_token = BEARER_TOKEN + "\n"
    connection = http.client.HTTPConnection("example.invalid")
    connection.putrequest("GET", "/")
    with pytest.raises(ValueError, match=BEARER_TOKEN):
        connection.putheader("Authorization", f"Bearer {newline_token}")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", newline_token)
    with pytest.raises(PalaceError, match="is not a valid bearer token") as invalid_token:
        embedder(bearer_store).probe()
    assert BEARER_TOKEN not in str(invalid_token.value) and len(stub.requests) == before
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")

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


class RecordingPacer:
    """A thread-safe pacer that admits at once and records every acquire and settle."""

    def __init__(self, *, refuse_on: int | None = None) -> None:
        self.lock = threading.Lock()
        self.acquired: list[PacedRequest] = []
        self.settled: list[tuple[str, float | None]] = []
        self.open: set[int] = set()
        self.refuse_on = refuse_on

    def acquire(self, request: PacedRequest, *, deadline: float | None, cancelled: Any) -> object:
        with self.lock:
            self.acquired.append(request)
            if self.refuse_on is not None and len(self.acquired) >= self.refuse_on:
                raise PacerRefused("fixture")
            reservation = object()
            self.open.add(id(reservation))
            return reservation

    def settle(self, reservation: object, outcome: str, *, retry_after: float | None) -> None:
        with self.lock:
            self.open.remove(id(reservation))  # exactly once: a second settle raises
            self.settled.append((outcome, retry_after))


def test_bedrock_failures_are_typed_paced_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_outbound_aws: Path
) -> None:
    """Every Bedrock failure ends typed with merged delivery; Retry-After never retries
    early; scopes bound waits, attempt starts and each read; a shared pacer sees every
    physical attempt with its byte size; partial success and usage stay truthful."""
    fixture_credentials(monkeypatch)
    stub = BedrockStub()
    stub.install(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(bedrock, "_sleep", sleeps.append)
    store = tmp_path / "store"

    def make(**options: Any) -> BedrockEmbedder:
        purpose = options.pop("purpose", "document")
        return BedrockEmbedder(
            store=store,
            model=MODEL,
            region=REGION,
            dim=DIM,
            profile=None,
            purpose=purpose,
            **options,
        )

    def invoke_audit() -> list[dict[str, Any]]:
        return [r for r in audit(store) if r["kind"] == "cloud_embed_call"]

    def fails(embedder: BedrockEmbedder, texts: list[str]) -> ProviderCallError:
        before = len(stub.invokes())
        with pytest.raises(ProviderCallError) as raised:
            embedder.embed(texts)
        raised.value.sent = len(stub.invokes()) - before  # type: ignore[attr-defined]
        return raised.value

    ready = make()
    ready.probe()

    # Four throttles: typed, still an IndexError, the message unchanged, every attempt audited,
    # each backoff inside its jitter bounds and recorded on the next attempt's audit record.
    usage = EmbeddingUsage()
    stub.errors = [(429, "ThrottlingException")] * 4
    before = len(invoke_audit())
    with usage_scope(usage):
        error = fails(ready, ["throttled"])
    assert isinstance(error, ProviderThrottledError) and isinstance(error, PalaceError)
    assert str(error) == "bedrock InvokeModel failed in us-east-1: ThrottlingException"
    assert (
        error.kind,
        error.delivery,
        error.attempts,
        error.stop,
        error.code,
        error.provider,
        error.operation,
        error.completed_inputs,
        error.retry_after,
        error.throttled,
    ) == (
        "throttled",
        "rejected",
        4,
        "attempts-exhausted",
        "ThrottlingException",
        "bedrock:us-east-1",
        "InvokeModel",
        0,
        None,
        True,
    )
    records = invoke_audit()[before:]
    assert [(r["attempt"], r["status"]) for r in records] == [
        (n, "ThrottlingException") for n in (1, 2, 3, 4)
    ]
    assert len(sleeps) == 3
    assert all(0.5 * 2**n <= delay <= 2**n for n, delay in enumerate(sleeps))
    assert [r["backoff_ms"] for r in records] == [0] + [round(d * 1000) for d in sleeps]
    summary = usage.summary()
    assert (
        summary.requests,
        summary.failed_requests,
        summary.throttled_requests,
        summary.indeterminate_requests,
    ) == (4, 4, 4, 0)

    # Each failure's kind and delivery; definite refusals stop at once.
    timeout = ReadTimeoutError(endpoint_url=RUNTIME_HOST)
    unreachable = EndpointConnectionError(endpoint_url=RUNTIME_HOST)
    closed = ConnectionClosedError(endpoint_url=RUNTIME_HOST)
    table: list[tuple[list[Any], str, str, int, str]] = [
        ([(400, "ValidationException")], "rejected", "rejected", 1, "not-retryable"),
        ([(403, "AccessDeniedException")], "rejected", "rejected", 1, "not-retryable"),
        ([(404, "ResourceNotFoundException")], "rejected", "rejected", 1, "not-retryable"),
        ([(400, "ServiceQuotaExceededException")], "rejected", "rejected", 1, "not-retryable"),
        # Failed while the model processed the request: possibly performed, never rejected.
        ([(424, "ModelErrorException")], "unavailable", "unknown", 1, "not-retryable"),
        # A code the pinned model does not name is not known to be a refusal.
        ([(418, "UnmodeledFixtureException")], "unavailable", "unknown", 1, "not-retryable"),
        (
            [(503, "ServiceUnavailableException")] * 4,
            "unavailable",
            "rejected",
            4,
            "attempts-exhausted",
        ),
        ([(500, "InternalServerException")] * 4, "unavailable", "unknown", 4, "attempts-exhausted"),
        ([timeout] * 4, "timeout", "unknown", 4, "attempts-exhausted"),
        ([unreachable] * 4, "connection", "not-sent", 4, "attempts-exhausted"),
        ([closed] * 4, "connection", "unknown", 4, "attempts-exhausted"),
    ]
    # Every error code the pinned models declare is classified deliberately: retryable,
    # a documented refusal, or the one documented as failing while the model processed.
    session = boto3.Session(region_name=REGION)._session
    declared = {
        shape.name
        for service, operation in (
            ("bedrock-runtime", "InvokeModel"),
            ("bedrock", "GetAccountDataRetention"),
        )
        for shape in session.get_service_model(service).operation_model(operation).error_shapes
    }
    assert declared - bedrock.BEDROCK_RETRYABLE_CODES - bedrock._DEFINITE_REFUSALS == {
        "ModelErrorException"
    }
    assert declared.issubset(bedrock._KNOWN_CODES)
    for errors, kind, delivery, attempts, stop in table:
        stub.errors = list(errors)
        error = fails(ready, ["classified"])
        assert type(error) is ProviderCallError
        assert (error.kind, error.delivery, error.attempts, error.stop) == (
            kind,
            delivery,
            attempts,
            stop,
        ), errors
        assert error.sent == attempts  # type: ignore[attr-defined]

    # Earlier uncertainty survives a later definite answer, whichever comes last.
    usage = EmbeddingUsage()
    stub.errors = [timeout] + [(429, "ThrottlingException")] * 3
    with usage_scope(usage):
        error = fails(ready, ["timeout then throttle"])
    assert isinstance(error, ProviderThrottledError)
    assert (error.kind, error.delivery, error.throttled) == ("throttled", "unknown", True)
    assert [item.kind for item in error.history] == ["timeout"] + ["throttled"] * 3
    assert (usage.summary().throttled_requests, usage.summary().indeterminate_requests) == (3, 1)
    stub.errors = [(429, "ThrottlingException")] + [timeout] * 3
    error = fails(ready, ["throttle then timeout"])
    assert type(error) is ProviderCallError
    assert (error.kind, error.delivery, error.throttled) == ("timeout", "unknown", True)
    # A pacer refusal after an indeterminate attempt keeps the uncertainty.
    refusing = RecordingPacer(refuse_on=2)
    paced = make(pacer=refusing)
    paced.probe()
    stub.errors = [timeout]
    error = fails(paced, ["timeout then refusal"])
    assert (error.kind, error.delivery, error.attempts, error.stop) == (
        "pacer-refused",
        "unknown",
        1,
        "pacer-refused",
    )
    assert error.sent == 1 and refusing.settled == [("failed", None)]  # type: ignore[attr-defined]
    assert isinstance(error.__cause__, PacerRefused)
    # A pacer that refuses at once: nothing sent, nothing audited.
    before = len(audit(store))
    error = fails(make(pacer=RecordingPacer(refuse_on=1)), ["refused"])
    assert (error.kind, error.delivery, error.attempts) == ("pacer-refused", "not-sent", 0)
    assert error.sent == 0  # type: ignore[attr-defined]
    assert [r["kind"] for r in audit(store)[before:]] == ["bedrock_retention_check"]

    # Retry-After: a hint beyond the cap ends the request at once with the hint, uncapped.
    before_sleeps = len(sleeps)
    stub.errors = [(429, "ThrottlingException", {"Retry-After": "120"})]
    error = fails(ready, ["long hint"])
    assert isinstance(error, ProviderThrottledError)
    assert (error.retry_after, error.stop, error.attempts) == (120.0, "hint-beyond-cap", 1)
    assert error.retry_not_before is not None and len(sleeps) == before_sleeps
    # A short hint is a floor that jitter only raises; an HTTP-date is read against Date.
    for rng, headers, expected in (
        (1.0, {"Retry-After": "2"}, 3.0),
        (0.0, {"Retry-After": "2"}, 2.0),
        (
            0.0,
            {
                "Retry-After": "Wed, 18 Nov 2026 00:00:10 GMT",
                "Date": "Wed, 18 Nov 2026 00:00:00 GMT",
            },
            10.0,
        ),
    ):
        hinted = make(_rng=lambda value=rng: value)
        stub.errors = [(429, "ThrottlingException", headers)]
        assert hinted.embed(["hinted"]) == [vector_for("hinted")]
        assert sleeps[-1] == expected
    # An HTTP-date hint without a readable Date is measured against the injected UTC clock:
    # beyond the cap it stops at once with the hint; within it, no retry before the hint.
    fixed_now = datetime(2026, 11, 18, 0, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(bedrock, "_utcnow", lambda: fixed_now)
    for extra in ({}, {"Date": "not a date"}):
        before_sleeps = len(sleeps)
        stub.errors = [
            (429, "ThrottlingException", {"Retry-After": "Wed, 18 Nov 2026 00:01:30 GMT", **extra})
        ]
        error = fails(ready, ["dated hint"])
        assert (error.retry_after, error.stop, error.attempts) == (90.0, "hint-beyond-cap", 1)
        assert len(sleeps) == before_sleeps
        stub.errors = [
            (429, "ThrottlingException", {"Retry-After": "Wed, 18 Nov 2026 00:00:20 GMT", **extra})
        ]
        before = len(stub.invokes())
        assert ready.embed(["dated hint"]) == [vector_for("dated hint")]
        assert len(stub.invokes()) - before == 2 and 20.0 <= sleeps[-1] <= 30.0
    # A hint the caller's deadline cannot accommodate stops now rather than sleeping into it.
    before_sleeps = len(sleeps)
    stub.errors = [(429, "ThrottlingException", {"Retry-After": "10"})]
    with call_scope(deadline=time.monotonic() + 5):
        error = fails(ready, ["deadline hint"])
    assert (error.kind, error.stop, error.retry_after, error.attempts) == (
        "throttled",
        "beyond-deadline",
        10.0,
        1,
    )
    assert len(sleeps) == before_sleeps

    # A passed deadline starts no attempt; an initialization queued behind another caller
    # stops at its own deadline or cancellation, typed, with nothing sent.
    before = len(audit(store))
    with call_scope(deadline=time.monotonic() - 1):
        error = fails(ready, ["late"])
    assert (error.kind, error.delivery, error.attempts, error.stop) == (
        "deadline",
        "not-sent",
        0,
        "interrupted",
    )
    assert error.sent == 0 and len(audit(store)) == before  # type: ignore[attr-defined]
    contended = make()
    contended._lock.acquire()
    try:
        for scope, kind in (
            (call_scope(deadline=time.monotonic() + 0.2), "deadline"),
            (call_scope(cancelled=lambda: True), "cancelled"),
        ):
            requests_before, started = len(stub.requests), time.monotonic()
            with scope, pytest.raises(ProviderCallError) as raised:
                contended.embed(["contended"])
            assert time.monotonic() - started < 5
            assert (raised.value.kind, raised.value.operation, raised.value.attempts) == (
                kind,
                "initialize",
                0,
            )
            assert len(stub.requests) == requests_before
    finally:
        contended._lock.release()
    # Cancellation during a backoff stops it, keeps what the attempt established, and makes
    # no further attempt.
    cancel = threading.Event()

    def cancelling_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        cancel.set()

    monkeypatch.setattr(bedrock, "_sleep", cancelling_sleep)
    stub.errors = [(429, "ThrottlingException")]
    with call_scope(cancelled=cancel.is_set):
        error = fails(ready, ["cancelled"])
    assert type(error) is ProviderCallError
    assert (error.kind, error.delivery, error.attempts, error.stop, error.throttled) == (
        "cancelled",
        "rejected",
        1,
        "interrupted",
        True,
    )
    assert error.sent == 1  # type: ignore[attr-defined]
    monkeypatch.setattr(bedrock, "_sleep", sleeps.append)

    # Each request's read timeout is the scope's remaining time, set per request; the shared
    # client keeps its configured timeouts, and concurrent calls see their own deadlines.
    with call_scope(deadline=time.monotonic() + 5):
        ready.embed(["bounded read"])
    assert 4 < stub.invokes()[-1].context["read_timeout"] <= 5
    ready.embed(["default read"])
    assert "read_timeout" not in stub.invokes()[-1].context
    runtime = ready._ready()
    assert (runtime.meta.config.connect_timeout, runtime.meta.config.read_timeout) == (60.0, 60.0)
    meet = threading.Barrier(2, timeout=10)

    def rendezvous(_request: Any) -> None:
        meet.wait()

    stub.hook = rendezvous
    seen: dict[str, float] = {}

    def bounded(seconds: float) -> None:
        with call_scope(deadline=time.monotonic() + seconds):
            ready.embed([f"deadline {seconds}"])

    workers = [threading.Thread(target=bounded, args=(s,)) for s in (5.0, 50.0)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    stub.hook = None
    for request in stub.invokes()[-2:]:
        seen[json.loads(request.body)["texts"][0]] = request.context["read_timeout"]
    assert 4 < seen["deadline 5.0"] <= 5 and 49 < seen["deadline 50.0"] <= 50

    # The pacer sees one acquire per physical attempt with its byte size, and exactly one
    # settle each; the retention read is not paced.
    pacer = RecordingPacer()
    query = make(pacer=pacer, purpose="query")
    texts = [f"{i}:" + "漢字🙂" * 10 for i in range(96)] + ["e\u0301" * 30, "\u0344" * 20]
    stub.errors = [None, (429, "ThrottlingException")]
    assert query.embed(texts) == [vector_for(text) for text in texts]
    first, second = texts[:96], texts[96:]
    assert [(r.attempt, r.inputs, r.request_bytes) for r in pacer.acquired] == [
        (1, 96, sum(map(request_units, first))),
        (1, 2, sum(map(request_units, second))),
        (2, 2, sum(map(request_units, second))),
    ]
    assert sum(map(request_units, second)) != sum(map(len, second))
    assert {(r.provider, r.model, r.operation, r.purpose) for r in pacer.acquired} == {
        ("bedrock:us-east-1", MODEL, "InvokeModel", "query")
    }
    assert pacer.settled == [("ok", None), ("throttled", None), ("ok", None)]
    assert not pacer.open and all("pacer_wait_ms" in r for r in invoke_audit()[-3:])

    # Partial success: the answered leading inputs are counted, not returned.
    usage = EmbeddingUsage()
    stub.errors = [None, *[(429, "ThrottlingException")] * 4]
    before = len(invoke_audit())
    with usage_scope(usage):
        error = fails(ready, texts)
    assert isinstance(error, ProviderThrottledError) and error.completed_inputs == 96
    assert [r["status"] for r in invoke_audit()[before:]] == ["ok"] + ["ThrottlingException"] * 4
    summary = usage.summary()
    assert (summary.requests, summary.failed_requests, summary.throttled_requests) == (5, 4, 4)

    # An answered but unusable response is typed as processed and never retried.
    stub.body = lambda batch, dim: {"embeddings": [[0.0] * 8 for _ in batch]}
    error = fails(ready, ["unusable"])
    stub.body = None
    assert (error.kind, error.delivery, error.attempts, error.stop) == (
        "response-invalid",
        "processed",
        1,
        "not-retryable",
    )
    assert "has dim 8; expected 1536" in str(error)

    # Diagnostics are library-owned: provider text (an echoed input, a credential, a huge
    # multiline body), an undeclared code, transport text and a hostile retention mode reach
    # neither the error, its fields nor the audit.
    input_marker = "CORPUSMARKER-input-never-echoed"
    hostile = f"{input_marker}\nkey {BEARER_TOKEN}\n" + "line of provider text\n" * 5000
    markers = (input_marker, BEARER_TOKEN, "provider text", "MARKERHOST")
    stub.error_message = hostile
    host = "https://MARKERHOST.invalid"
    before_audit = len(audit(store))
    for errors, shown in (
        ([(400, "ValidationException")], "ValidationException"),
        ([(429, "ThrottlingException")] * 4, "ThrottlingException"),
        ([(400, input_marker.replace("-", ""))], "UnrecognizedServiceError"),
        ([EndpointConnectionError(endpoint_url=host)] * 4, "EndpointConnectionError"),
        ([ReadTimeoutError(endpoint_url=host)] * 4, "ReadTimeoutError"),
    ):
        stub.errors = list(errors)
        error = fails(ready, [input_marker])
        assert str(error) == f"bedrock InvokeModel failed in us-east-1: {shown}"
        fields = repr((error.code, error.provider, error.operation, error.history, error.args))
        assert not any(marker in fields for marker in markers)
    stub.error_message = "fixture"
    stub.mode = hostile
    with pytest.raises(PalaceError, match="is an unrecognized value; palace requires") as mode:
        make().probe()
    assert not any(marker in str(mode.value) for marker in markers)
    stub.mode = "none"
    written = json.dumps(audit(store)[before_audit:])
    assert not any(marker in written for marker in markers)
    assert "UnrecognizedServiceError" in written

    # Four threads share one embedder, one pacer and one usage total (each in its own copy
    # of the submitting context): no lost audit record, exact counts.
    import contextvars

    shared = RecordingPacer()
    worker_embedder = make(pacer=shared)
    worker_embedder.probe()
    usage = EmbeddingUsage()
    before = len(invoke_audit())
    with usage_scope(usage):
        contexts = [contextvars.copy_context() for _ in range(4)]
    answers: dict[int, list[list[float]]] = {}

    def work(index: int) -> None:
        answers[index] = worker_embedder.embed([f"worker {index} {j}" for j in range(3)])

    threads = [threading.Thread(target=contexts[i].run, args=(work, i)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(answers) == [0, 1, 2, 3]
    added = invoke_audit()[before:]
    assert len(added) == 4 and len({r["id"] for r in added}) == 4
    assert (usage.summary().requests, usage.summary().failed_requests) == (4, 0)
    assert len(shared.acquired) == 4 and shared.settled == [("ok", None)] * 4
    for embedder_ in (ready, paced, hinted, query, worker_embedder):
        embedder_.close()
