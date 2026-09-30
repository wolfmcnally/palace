"""Present-use provider registry and shared private-endpoint configuration."""

from __future__ import annotations

import hashlib
import math
import os
import re
import ssl
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from palace.daemons.capture.config import DEFAULT_STORE
from palace.index._errors import IndexError
from palace.vault import resolve_vault_root

PROVIDERS = {
    "embedding": ("ollama", "openrouter", "endpoint"),
    "reranking": ("local", "endpoint"),
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def require_provider(operation: str, provider: str) -> None:
    if provider not in PROVIDERS.get(operation, ()):
        raise IndexError("provider is not registered for this operation")


@dataclass(frozen=True)
class EndpointConfig:
    name: str
    url: str
    credential_env: str
    adapter: str
    authorized_boundary: str
    boundary_note: str
    boundary_asserted_at: str
    timeout_seconds: float = 30.0
    max_attempts: int = 2
    ca_bundle: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise IndexError("endpoint name must be a short identifier")
        if not isinstance(self.credential_env, str) or not _ENV.fullmatch(self.credential_env):
            raise IndexError("endpoint credential_env must name an environment variable")
        if not isinstance(self.url, str):
            raise IndexError("endpoint URL must be HTTPS")
        try:
            url = urlsplit(self.url)
            _ = url.port
        except ValueError:
            raise IndexError("endpoint URL is invalid") from None
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(char.isspace() for char in self.url)
        ):
            raise IndexError("endpoint requires HTTPS without URL credentials, query or fragment")
        if self.adapter != "palace-json-v1":
            raise IndexError("endpoint adapter must explicitly select palace-json-v1")
        if (
            self.authorized_boundary != "operator-controlled"
            or not isinstance(self.boundary_note, str)
            or not self.boundary_note.strip()
            or not isinstance(self.boundary_asserted_at, str)
        ):
            raise IndexError(
                "endpoint requires explicit operator-controlled boundary authorization"
            )
        try:
            stamp = datetime.fromisoformat(self.boundary_asserted_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise IndexError(
                "endpoint boundary_asserted_at must be a timestamp with a zone"
            ) from None
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not 0 < self.timeout_seconds <= 120
            or not math.isfinite(self.timeout_seconds)
        ):
            raise IndexError("endpoint timeout_seconds must be finite, positive and at most 120")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 3
        ):
            raise IndexError("endpoint max_attempts must be an integer from 1 to 3")
        if self.ca_bundle is not None and (
            not isinstance(self.ca_bundle, str)
            or not Path(self.ca_bundle).expanduser().is_absolute()
            or not Path(self.ca_bundle).expanduser().is_file()
        ):
            raise IndexError("endpoint ca_bundle must reference an existing absolute-path PEM file")

    @property
    def provider_id(self) -> str:
        stamp = hashlib.sha256(
            (self.url + "\n" + self.adapter + "\n" + self.credential_env).encode()
        ).hexdigest()[:24]
        return f"endpoint:{self.name}:{stamp}"

    def runtime(self) -> tuple[str, ssl.SSLContext]:
        """Validate every store's referenced material before any request starts."""
        secret = os.environ.get(self.credential_env)
        if not secret or not secret.strip() or "\r" in secret or "\n" in secret:
            raise IndexError("private inference credential reference is unavailable or invalid")
        try:
            context = ssl.create_default_context(
                cafile=str(Path(self.ca_bundle).expanduser()) if self.ca_bundle else None
            )
        except (OSError, ssl.SSLError):
            raise IndexError("private inference CA material could not be loaded") from None
        return secret, context

    def document(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def endpoint_config(value: object) -> EndpointConfig:
    if not isinstance(value, Mapping):
        raise IndexError("endpoint configuration requires an [endpoint] table")
    try:
        return EndpointConfig(**dict(value))
    except (TypeError, AttributeError):
        raise IndexError("endpoint configuration has missing, unknown or invalid fields") from None


def assert_private_boundary(*, store: Path, watch_roots: Sequence[Path]) -> None:
    """Keep personal-store and personal-vault protection independent of attestation."""
    assert_outside_personal_boundary(
        store=store, watch_roots=watch_roots, operation="private inference"
    )


def assert_outside_personal_boundary(
    *, store: Path, watch_roots: Sequence[Path], operation: str
) -> None:
    """Refuse ``operation`` for the personal store or a watch root overlapping the vault.

    Both containment directions are refused; no opt-in or attestation lifts it.
    """
    selected = store.expanduser().resolve()
    personal = DEFAULT_STORE.expanduser().resolve()
    if selected.is_relative_to(personal) or personal.is_relative_to(selected):
        raise IndexError(f"{operation} is forbidden for the personal store")
    vault = resolve_vault_root(None).expanduser().resolve()
    for watch_root in watch_roots:
        root = watch_root.expanduser().resolve()
        if root.is_relative_to(vault) or vault.is_relative_to(root):
            raise IndexError(
                f"{operation} is forbidden for a watch root overlapping the personal vault"
            )


def write_selector(path: Path, document: Mapping[str, object]) -> None:
    """Atomically replace a selector without serializing any credential values."""
    serialized = tomlkit.dumps(dict(document))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
