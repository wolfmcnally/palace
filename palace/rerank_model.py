"""Acquire and verify palace's shared local cross-encoder artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from palace.retrieval_config import rerank_model_dir

RERANK_MODEL_FILENAME: str = "model.onnx"
RERANK_TOKENIZER_FILENAME: str = "tokenizer.json"

__all__ = [
    "ModelArtifact",
    "ModelProvisionError",
    "ProvisionResult",
    "RERANK_ARTIFACTS",
    "RERANK_MODEL_FILENAME",
    "RERANK_TOKENIZER_FILENAME",
    "main",
    "provision_rerank_model",
]


@dataclass(frozen=True)
class ModelArtifact:
    """One immutable artifact in the pinned reranker model distribution."""

    filename: str
    url: str
    bytes: int
    sha256: str


RERANK_ARTIFACTS: tuple[ModelArtifact, ...] = (
    ModelArtifact(
        filename=RERANK_MODEL_FILENAME,
        url=(
            "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/"
            "resolve/main/onnx/model.onnx"
        ),
        bytes=91_011_230,
        sha256="5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a",
    ),
    ModelArtifact(
        filename=RERANK_TOKENIZER_FILENAME,
        url=(
            "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/"
            "resolve/main/tokenizer.json"
        ),
        bytes=711_396,
        sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    ),
)


class ModelProvisionError(RuntimeError):
    """A pinned artifact could not be acquired or verified safely."""


@dataclass(frozen=True)
class ProvisionResult:
    """Artifact dispositions from one provisioning pass."""

    model_dir: Path
    downloaded: tuple[str, ...]
    verified: tuple[str, ...]


Download = Callable[[str, Path], None]
FetchNotice = Callable[[Path], None]


def _sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _observed(file_path: Path) -> tuple[int, str]:
    try:
        return file_path.stat().st_size, _sha256(file_path)
    except OSError as exc:
        raise ModelProvisionError(f"cannot read {file_path.name}: {exc}") from exc


def _verify_existing(file_path: Path, artifact: ModelArtifact) -> None:
    observed_bytes, observed_digest = _observed(file_path)
    if observed_digest != artifact.sha256:
        raise ModelProvisionError(
            f"{artifact.filename} digest mismatch: expected {artifact.sha256}, "
            f"observed {observed_digest}; re-run with --force to re-download"
        )
    if observed_bytes != artifact.bytes:
        raise ModelProvisionError(
            f"{artifact.filename} size mismatch: expected {artifact.bytes} bytes, "
            f"observed {observed_bytes}; re-run with --force to re-download"
        )


def _download_url(url: str, destination: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=60) as response,  # noqa: S310
        destination.open("wb") as stream,
    ):
        shutil.copyfileobj(response, stream)


def _download_and_verify(
    *,
    artifact: ModelArtifact,
    model_dir: Path,
    downloader: Download,
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=model_dir,
            prefix=f".{artifact.filename}.",
            suffix=".part",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        downloader(artifact.url, temporary_path)
        observed_bytes, observed_digest = _observed(temporary_path)
        if observed_bytes != artifact.bytes or observed_digest != artifact.sha256:
            raise ModelProvisionError(
                f"downloaded {artifact.filename} failed verification: expected "
                f"{artifact.bytes} bytes sha256 {artifact.sha256}, observed "
                f"{observed_bytes} bytes sha256 {observed_digest}"
            )
        os.replace(temporary_path, model_dir / artifact.filename)
        temporary_path = None
    except ModelProvisionError:
        raise
    except Exception as exc:
        # Downloader failures span urllib, http.client, and injected transports.
        raise ModelProvisionError(f"failed to download {artifact.filename}: {exc}") from exc
    finally:
        if temporary_path is not None:
            # A unique .part file is never a valid artifact; preserve the
            # acquisition error if best-effort cleanup itself is unavailable.
            with contextlib.suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def provision_rerank_model(
    *,
    force: bool = False,
    on_fetch: FetchNotice | None = None,
    artifacts: Sequence[ModelArtifact] | None = None,
    downloader: Download | None = None,
) -> ProvisionResult:
    """Ensure the shared reranker is complete and digest-verified.

    Existing mismatching artifacts are refused unless ``force`` is explicit.
    Downloads are verified in a unique temporary file and atomically installed,
    leaving an existing artifact intact when acquisition fails.
    """
    model_dir = rerank_model_dir()
    artifacts = RERANK_ARTIFACTS if artifacts is None else artifacts
    downloader = _download_url if downloader is None else downloader
    missing = [artifact for artifact in artifacts if not (model_dir / artifact.filename).is_file()]
    if (force or missing) and on_fetch is not None:
        on_fetch(model_dir)
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelProvisionError(f"cannot create model directory {model_dir}: {exc}") from exc

    downloaded: list[str] = []
    verified: list[str] = []
    for artifact in artifacts:
        artifact_path = model_dir / artifact.filename
        if artifact_path.is_file() and not force:
            _verify_existing(artifact_path, artifact)
            verified.append(artifact.filename)
            continue
        _download_and_verify(
            artifact=artifact,
            model_dir=model_dir,
            downloader=downloader,
        )
        downloaded.append(artifact.filename)
    return ProvisionResult(
        model_dir=model_dir,
        downloaded=tuple(downloaded),
        verified=tuple(verified),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch and verify palace's shared pinned cross-encoder artifacts."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing artifacts after downloading and verifying fresh copies.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target = rerank_model_dir()
    print(f"rerank-model: directory={target}", flush=True)
    try:
        result = provision_rerank_model(force=args.force)
    except ModelProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    for filename in result.verified:
        print(f"rerank-model: {filename} already verified", flush=True)
    for filename in result.downloaded:
        print(f"rerank-model: {filename} downloaded and verified", flush=True)
    print("rerank-model: ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
