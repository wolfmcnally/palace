"""Shared fixtures for ``tests/index/``.

- :func:`tmp_store` — hermetic machine-state root rooted in ``tmp_path``.
- :func:`tmp_watch_root` — hermetic watch root rooted in ``tmp_path``.
- :func:`clean_env` — strips ``PALACE_STORE`` so the CLI tests see only
  the ``--store`` flag they pass.
- :func:`populated_config` — writes a watch-roots TOML carrying
  ``tmp_watch_root`` and returns the store path.
- :class:`FakeEmbedder` — a deterministic content-hashed embedder for
  every test except :mod:`tests.index.test_embedder` (which exercises
  the real :class:`OllamaEmbedder` via ``httpx.MockTransport``).
- :class:`FakeS3Vectors` — an in-memory stand-in for the boto3 ``s3vectors``
  client: same method names and keyword arguments, botocore ``ClientError``
  failures, a log of every request attempt, scripted failures, and query results
  in pages with ``nextToken``.
"""

from __future__ import annotations

import copy
import hashlib
import math
import struct
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from palace.index.config import EMBED_DIM
from palace.index.embedder_config import EmbedderConfig, save_embedder_config
from palace.watch.config import WatchRootsConfig, default_config_path

__all__ = [
    "FakeEmbedder",
    "FakeS3Vectors",
    "client_error",
    "clean_env",
    "local_store_with_toml",
    "populated_config",
    "remote_store",
    "tmp_store",
    "tmp_watch_root",
]


class FakeEmbedder:
    """Deterministic content-hashed embedder.

    Each input text maps to a vector derived from its SHA-256 digest: the
    digest is repeated and unpacked into ``EMBED_DIM`` little-endian
    f32 values, mod-normalized to roughly unit range. The same body
    always produces the same vector — required by tests that re-index
    an unchanged file and want zero embedder calls (the writer's
    file-hash short-circuit fires before ``embed`` is ever reached).
    """

    def __init__(
        self,
        dim: int = EMBED_DIM,
        *,
        max_concurrency: int = 1,
        latency_seconds: float = 0.0,
    ) -> None:
        self.dim: int = dim
        self.max_concurrency = max_concurrency
        self.latency_seconds = latency_seconds
        self.call_count: int = 0
        self.last_batch: list[str] | None = None
        self.all_batches: list[list[str]] = []
        self.raise_on_call: Exception | None = None
        self.thread_ids: list[int] = []
        self._record_lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        with self._record_lock:
            self.call_count += 1
            self.thread_ids.append(threading.get_ident())
            self.last_batch = batch
            self.all_batches.append(batch)
            raise_on_call = self.raise_on_call
        if raise_on_call is not None:
            raise raise_on_call
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        return [self._vector_for(text) for text in batch]

    def probe(self) -> None:
        return None

    def close(self) -> None:
        return None

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 bytes → 8 f32 floats. Repeat to fill ``self.dim``.
        unit = list(struct.unpack("<8f", digest))
        # The unpacked f32s span the full float range; normalize to a
        # tame magnitude so a later test can do approximate-equality
        # assertions without dealing with denormals.
        scaled = [self._tame(x) for x in unit]
        repeats = (self.dim + len(scaled) - 1) // len(scaled)
        vector = (scaled * repeats)[: self.dim]
        return vector

    @staticmethod
    def _tame(value: float) -> float:
        # Clamp / normalize: NaN → 0, Inf → ±1, else fold into [-1, 1].
        if value != value:  # NaN
            return 0.0
        if value == float("inf"):
            return 1.0
        if value == float("-inf"):
            return -1.0
        # Round-trip through a stable hash-style fold to bring the value
        # into roughly [-1, 1] without losing the per-input variance.
        # We treat the f32 bits as an integer and divide by 2**31.
        bits = struct.unpack("<i", struct.pack("<f", value))[0]
        return float(bits) / float(2**31)


def client_error(code: str, operation: str) -> ClientError:
    """A botocore failure carrying ``code``, as the service raises it."""
    return ClientError({"Error": {"Code": code, "Message": f"fixture {code}"}}, operation)


class FakeS3Vectors:
    """In-memory S3 Vectors: buckets, indexes and vectors behind the boto3 method names.

    ``calls`` logs every request attempt as ``(method, params)`` before it succeeds or
    fails. ``failures`` scripts outcomes per method: each attempt pops the next entry and
    raises it when it is an exception (``None`` lets that attempt through). Queries return
    ``ranking`` when set, else every key by cosine distance, ``page_size`` per page.
    """

    def __init__(self, account: str = "123456789012", region: str = "us-east-1") -> None:
        self.account = account
        self.region = region
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.buckets: set[str] = set()
        self.indexes: dict[str, dict[str, Any]] = {}
        self.vectors: dict[str, dict[str, dict[str, Any]]] = {}
        self.failures: dict[str, list[Exception | None]] = {}
        self.ranking: list[tuple[str, float]] | None = None
        self.page_size = 100
        self.list_page_size = 1000

    def arn(self, bucket: str, index: str) -> str:
        return f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/{bucket}/index/{index}"

    def add_index(self, bucket: str, index: str, **config: Any) -> str:
        arn = self.arn(bucket, index)
        self.buckets.add(bucket)
        self.indexes[arn] = {"vectorBucketName": bucket, "indexName": index, "indexArn": arn}
        self.indexes[arn].update(config)
        self.vectors.setdefault(arn, {})
        return arn

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def _attempt(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, copy.deepcopy(params)))
        scripted = self.failures.get(method)
        if scripted:
            outcome = scripted.pop(0)
            if outcome is not None:
                raise outcome

    @staticmethod
    def _ok(**body: Any) -> dict[str, Any]:
        return {**body, "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}

    def get_vector_bucket(self, **params: Any) -> dict[str, Any]:
        self._attempt("get_vector_bucket", params)
        name = params["vectorBucketName"]
        if name not in self.buckets:
            raise client_error("NotFoundException", "GetVectorBucket")
        return self._ok(vectorBucket={"vectorBucketName": name})

    def create_vector_bucket(self, **params: Any) -> dict[str, Any]:
        self._attempt("create_vector_bucket", params)
        self.buckets.add(params["vectorBucketName"])
        return self._ok()

    def get_index(self, **params: Any) -> dict[str, Any]:
        self._attempt("get_index", params)
        arn = self.arn(params["vectorBucketName"], params["indexName"])
        if arn not in self.indexes:
            raise client_error("NotFoundException", "GetIndex")
        return self._ok(index=copy.deepcopy(self.indexes[arn]))

    def create_index(self, **params: Any) -> dict[str, Any]:
        self._attempt("create_index", params)
        bucket, index = params["vectorBucketName"], params["indexName"]
        config = {
            key: value
            for key, value in params.items()
            if key not in {"vectorBucketName", "indexName"}
        }
        return self._ok(indexArn=self.add_index(bucket, index, **config))

    def put_vectors(self, **params: Any) -> dict[str, Any]:
        self._attempt("put_vectors", params)
        stored = self.vectors[params["indexArn"]]
        for item in params["vectors"]:
            stored[item["key"]] = {
                "data": list(item["data"]["float32"]),
                "metadata": dict(item.get("metadata") or {}),
            }
        return self._ok()

    def delete_vectors(self, **params: Any) -> dict[str, Any]:
        self._attempt("delete_vectors", params)
        stored = self.vectors[params["indexArn"]]
        for key in params["keys"]:
            stored.pop(key, None)
        return self._ok()

    def list_vectors(self, **params: Any) -> dict[str, Any]:
        self._attempt("list_vectors", params)
        keys = sorted(self.vectors[params["indexArn"]])
        start = int(params.get("nextToken") or 0)
        page = keys[start : start + min(params["maxResults"], self.list_page_size)]
        body: dict[str, Any] = {"vectors": [{"key": key} for key in page]}
        if start + len(page) < len(keys):
            body["nextToken"] = str(start + len(page))
        return self._ok(**body)

    def query_vectors(self, **params: Any) -> dict[str, Any]:
        self._attempt("query_vectors", params)
        stored = self.vectors[params["indexArn"]]
        if self.ranking is not None:
            ranked = list(self.ranking)
        else:
            query = params["queryVector"]["float32"]
            ranked = sorted(
                ((key, _cosine_distance(query, value["data"])) for key, value in stored.items()),
                key=lambda row: (row[1], row[0]),
            )
        ranked = ranked[: params["topK"]]
        start = int(params.get("nextToken") or 0)
        page = ranked[start : start + self.page_size]
        vectors = []
        for key, distance in page:
            item: dict[str, Any] = {"key": key, "distance": distance}
            if params.get("returnMetadata") and key in stored:
                item["metadata"] = dict(stored[key]["metadata"])
            vectors.append(item)
        body: dict[str, Any] = {"vectors": vectors, "distanceMetric": "cosine"}
        if start + len(page) < len(ranked):
            body["nextToken"] = str(start + len(page))
        return self._ok(**body)


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return 1.0 - dot / norm if norm else 1.0


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    """A hermetic machine-state root rooted in pytest's ``tmp_path``."""
    store = tmp_path / "store"
    store.mkdir()
    return store


@pytest.fixture
def tmp_watch_root(tmp_path: Path) -> Path:
    """A hermetic watch root rooted in pytest's ``tmp_path``."""
    root = tmp_path / "watch"
    root.mkdir()
    return root


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env vars that would override the CLI's ``--store`` flag."""
    monkeypatch.delenv("PALACE_STORE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    yield


@pytest.fixture
def populated_config(tmp_store: Path, tmp_watch_root: Path) -> Path:
    """Write a watch-roots TOML for ``tmp_watch_root`` and return ``tmp_store``."""
    config_path = default_config_path(tmp_store)
    config = WatchRootsConfig().with_added(tmp_watch_root, store_root=tmp_store)
    config.save(config_path)
    return tmp_store


@pytest.fixture
def embedder() -> FakeEmbedder:
    """Return a fresh :class:`FakeEmbedder` instance."""
    return FakeEmbedder()


@pytest.fixture
def remote_store(tmp_store: Path) -> Path:
    save_embedder_config(
        tmp_store,
        EmbedderConfig(
            provider="openrouter",
            model="Qwen/Qwen3-Embedding-8B",
            upstream="DeepInfra",
            dim=EMBED_DIM,
            published_corpus=True,
            published_corpus_note="Published fixture corpus",
            published_corpus_asserted_at="2026-08-08T12:00:00-06:00",
        ),
    )
    return tmp_store


@pytest.fixture
def local_store_with_toml(tmp_store: Path) -> Path:
    save_embedder_config(
        tmp_store,
        EmbedderConfig(provider="ollama", model="qwen3-embedding:8b", dim=EMBED_DIM),
    )
    return tmp_store


@pytest.fixture
def fast_park(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("palace.index.server.PARK_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr("palace.index.server.WRITER_READY_TIMEOUT_SECONDS", 0.05)
