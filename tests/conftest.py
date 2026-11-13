"""Suite-wide safety guards for tests that can reach shared runtime state."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import palace.index.embedder as embedder_module
import palace.rerank_model as rerank_model_module

# One front-matter value per loader failure class ``yaml.safe_load`` raises for
# input: a syntax error (``yaml.YAMLError``), a calendar-invalid date and an
# over-long integer (``ValueError``), over-deep nesting (``RecursionError``), an
# explicit ``!!bool`` / ``!!int`` tag on an unrecognized or empty value
# (``KeyError`` / ``IndexError``), an explicit ``!!timestamp`` tag on a
# non-timestamp (``AttributeError``) and on a value-key mapping (``TypeError``),
# and a long sexagesimal float (``OverflowError``).
UNLOADABLE_FRONTMATTER_VALUES: tuple[str, ...] = (
    "[unclosed",
    "2026-02-30",
    "9" * 5000,
    "[" * 5000 + "]" * 5000,
    "!!bool invalid",
    '!!int ""',
    "!!timestamp invalid",
    "!!timestamp {=: 2026-01-01}",
    "1:" * 200 + "1.0",
)


def aws_env_credential_names() -> tuple[str, ...]:
    """Every variable botocore's ``EnvProvider`` reads, from the installed SDK itself."""
    from botocore.credentials import EnvProvider

    return (
        EnvProvider.ACCESS_KEY,
        EnvProvider.SECRET_KEY,
        *EnvProvider.TOKENS,
        EnvProvider.EXPIRY_TIME,
        EnvProvider.ACCOUNT_ID,
    )


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


# Every ambient source the installed botocore consults for credentials, bearer tokens and
# endpoint overrides; the guard removes them before any test constructs a session. The
# environment provider's inputs come from its own class attributes, so the inventory
# follows the installed SDK (keys, AWS_SECURITY_TOKEN/AWS_SESSION_TOKEN, expiry, account).
_AWS_AMBIENT = (
    *aws_env_credential_names(),
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CREDENTIAL_FILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_BEDROCK",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
)


@pytest.fixture(scope="session", autouse=True)
def guard_outbound_aws(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Isolate AWS credentials and endpoints, and fail any request a stub did not answer.

    A ``before-send`` stub answers a request before botocore's transport is reached, so
    only an unstubbed request (instance-metadata credential fetches included) arrives at
    the replaced ``URLLib3Session.send``. Yields the directory the redirected AWS config
    paths point into, none of which exist.
    """
    from botocore.httpsession import URLLib3Session

    isolated = tmp_path_factory.mktemp("palace-test-aws")
    patcher = pytest.MonkeyPatch()
    for name in _AWS_AMBIENT:
        patcher.delenv(name, raising=False)
    patcher.setenv("AWS_CONFIG_FILE", str(isolated / "config"))
    patcher.setenv("AWS_SHARED_CREDENTIALS_FILE", str(isolated / "credentials"))
    patcher.setenv("BOTO_CONFIG", str(isolated / "boto"))
    patcher.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def reject(self: object, request: object) -> object:
        pytest.fail(
            "test attempted a real outbound AWS request: "
            f"{getattr(request, 'method', '?')} {getattr(request, 'url', '?')}"
        )

    patcher.setattr(URLLib3Session, "send", reject)
    try:
        yield isolated
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
