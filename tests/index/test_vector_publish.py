"""Publishing a store's chunk vectors to S3 Vectors, incrementally by chunk key."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec

import palace.index.s3vectors as s3v
from palace.cli import main as palace_main
from palace.index._errors import IndexError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.embedder_config import EmbedderConfig, resolve_identity, save_embedder_config
from palace.index.update import update
from palace.watch.config import WatchRootsConfig, default_config_path

from .conftest import FakeEmbedder, FakeS3Vectors, client_error

BUCKET, INDEX, REGION = "fixture-vectors", "fixture-index", "us-east-1"
REAL_OPEN_CLIENT = s3v.open_client
FILES = {
    "a.md": "# Alpha\n\nalpha orchard lantern notes\n\n## Second\n\nalpha second section\n",
    "b.md": "# Beta\n\nbeta harbor lantern notes\n",
    "c.md": '# Gamma\n\ngamma quarry "quoted" notes\n',
}
CAMEL = {
    "get_vector_bucket": "GetVectorBucket",
    "create_vector_bucket": "CreateVectorBucket",
    "get_index": "GetIndex",
    "create_index": "CreateIndex",
    "put_vectors": "PutVectors",
    "delete_vectors": "DeleteVectors",
    "list_vectors": "ListVectors",
    "query_vectors": "QueryVectors",
}


class Negated(FakeEmbedder):
    """Another embedding space: the same texts, opposite vectors."""

    def _vector_for(self, text: str) -> list[float]:
        return [-value for value in super()._vector_for(text)]


def _connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(chunks_db_path(store)))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _store_vectors(store: Path) -> dict[str, list[float]]:
    with closing(_connect(store)) as conn:
        return {
            key: list(struct.unpack(f"<{EMBED_DIM}f", blob))
            for key, blob in conn.execute("SELECT chunk_id, embedding FROM chunks_vec")
        }


def _store_metadata(store: Path) -> dict[str, dict[str, str]]:
    with closing(_connect(store)) as conn:
        return {
            key: {"text": body, "path": path, "heading": heading or ""}
            for key, body, path, heading in conn.execute(
                "SELECT chunk_id, body, path, heading FROM chunks"
            )
        }


def _audit(store: Path) -> list[dict[str, Any]]:
    records = []
    for day in sorted((store / "events" / "cloud-egress").glob("*.jsonl")):
        for line in day.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["kind"] == "cloud_vector_call":
                records.append(record)
    return records


def _publish(store: Path, **kwargs: Any) -> s3v.PublishResult:
    return s3v.publish_vectors(
        store=store, bucket=BUCKET, index=INDEX, region=REGION, profile="fixture", **kwargs
    )


def _remote(fake: FakeS3Vectors) -> dict[str, dict[str, Any]]:
    return fake.vectors[fake.arn(BUCKET, INDEX)]


def test_publish_vectors_incrementally_by_chunk_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, root = tmp_path / "store", tmp_path / "watch"
    root.mkdir()
    for name, body in FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    store.mkdir()
    WatchRootsConfig().with_added(root, store_root=store).save(default_config_path(store))
    build(store=store, watch_root_filter=root, full=True, embedder=FakeEmbedder())
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("PALACE_STORE", raising=False)
    fake = FakeS3Vectors()
    opened: list[tuple[str | None, str]] = []

    def open_fake(profile: str | None, region: str) -> FakeS3Vectors:
        opened.append((profile, region))
        return fake

    sleeps: list[float] = []
    monkeypatch.setattr(s3v, "open_client", open_fake)
    monkeypatch.setattr(s3v, "_sleep", sleeps.append)

    # Refusals before any request: no opt-in, an incomplete one, the personal store, a
    # watch root overlapping the vault, and text metadata over the per-vector limit.
    with pytest.raises(IndexError, match="not opted in as published") as refused:
        _publish(store)
    assert refused.value.vector_usage == s3v.VectorUsageSummary()  # type: ignore[attr-defined]
    opt_in = store / "meta" / "vector-publishing.toml"
    opt_in.write_text(
        'published_corpus = false\npublished_corpus_note = "x"\n'
        'published_corpus_asserted_at = "2026-09-28T12:00:00-06:00"\n',
        encoding="utf-8",
    )
    with pytest.raises(IndexError, match="must assert published_corpus = true"):
        _publish(store)
    monkeypatch.setattr("palace.provider_config.DEFAULT_STORE", store)
    with pytest.raises(IndexError, match="vector publishing is forbidden for the personal store"):
        s3v.save_publishing_config(store, note="fixture corpus, published to a fixture index")
    monkeypatch.undo()
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setattr(s3v, "open_client", open_fake)
    monkeypatch.setattr(s3v, "_sleep", sleeps.append)
    s3v.save_publishing_config(store, note="fixture corpus, published to a fixture index")
    assert s3v.load_publishing_config(store) is not None
    monkeypatch.setattr("palace.provider_config.DEFAULT_STORE", store)
    with pytest.raises(IndexError, match="vector publishing is forbidden for the personal store"):
        _publish(store)
    monkeypatch.setattr("palace.provider_config.DEFAULT_STORE", tmp_path / "personal")
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(root / "notes"))
    with pytest.raises(IndexError, match="overlapping the personal vault"):
        _publish(store)
    monkeypatch.setenv("PALACE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setattr(s3v, "MAX_METADATA_BYTES", 40)
    with pytest.raises(IndexError, match="exceed the 40-byte text-metadata limit"):
        _publish(store, text_metadata=True)
    monkeypatch.setattr(s3v, "MAX_METADATA_BYTES", 40_000)
    assert fake.calls == [] and opened == []

    # An existing index of another dimension or distance is refused before any write.
    for config, problem in (
        ({"dataType": "float32", "dimension": 8, "distanceMetric": "cosine"}, "dimension 8"),
        (
            {"dataType": "float32", "dimension": EMBED_DIM, "distanceMetric": "euclidean"},
            "distance 'euclidean'",
        ),
    ):
        fake = FakeS3Vectors()
        fake.add_index(BUCKET, INDEX, **config)
        with pytest.raises(IndexError, match=problem):
            _publish(store)
        assert fake.methods() == ["get_vector_bucket", "get_index"]
    assert not (store / "meta" / "vector-publication.json").exists()

    # First publish: bucket and index created; every chunk vector put, byte-exact.
    fake = FakeS3Vectors()
    monkeypatch.setattr(s3v, "REQUEST_BUDGET_BYTES", 150_000)  # one vector per request
    vectors = _store_vectors(store)
    refused_records = _audit(store)
    assert [r["operation"] for r in refused_records] == ["GetVectorBucket", "GetIndex"] * 2
    first = _publish(store)
    assert (first.created_bucket, first.created_index, first.listed_remote) == (True, True, False)
    assert (first.put, first.deleted, first.vector_count) == (len(vectors), 0, len(vectors))
    create = dict(fake.calls)["create_index"]
    assert create["dimension"] == EMBED_DIM and create["distanceMetric"] == "cosine"
    assert create["dataType"] == "float32"
    assert create["metadataConfiguration"] == {
        "nonFilterableMetadataKeys": ["text", "path", "heading"]
    }
    assert {key: value["data"] for key, value in _remote(fake).items()} == vectors
    puts = [params for method, params in fake.calls if method == "put_vectors"]
    assert len(puts) == len(vectors) > 1
    receipt = json.loads((store / "meta" / "vector-publication.json").read_text())
    expected_digest = hashlib.sha256(
        "".join(f"{k}\n" for k in sorted(vectors)).encode()
    ).hexdigest()
    identity = resolve_identity(store)
    assert receipt["state"] == "complete" and receipt["index_arn"] == fake.arn(BUCKET, INDEX)
    assert receipt["key_set_sha256"] == expected_digest and receipt["vector_count"] == len(vectors)
    assert receipt["embedder"] == {
        "convention": identity.convention,
        "model": identity.model,
        "provider": identity.provider,
        "dim": identity.dim,
    }
    assert (store / "meta" / "vector-publication-keys.txt").read_text().split() == sorted(vectors)
    assert opened[-1] == ("fixture", REGION)

    # Audit and usage: one record per request attempt, in order, no corpus text.
    records = _audit(store)[len(refused_records) :]
    assert [record["operation"] for record in records] == [
        CAMEL[method] for method in fake.methods()
    ]
    assert [r["chunk_ids"] for r in records if r["operation"] == "PutVectors"] == [
        [item["key"] for item in params["vectors"]] for params in puts
    ]
    assert first.usage.requests == len(records)
    assert first.usage.failed_requests == 0  # the two not-found probes are expected
    assert first.usage.request_bytes == sum(record["request_bytes"] for record in records)
    assert all(r["request_bytes"] <= s3v.MAX_REQUEST_BYTES for r in records)
    audit_bytes = b"".join(p.read_bytes() for p in (store / "events" / "cloud-egress").iterdir())
    assert b"lantern" not in audit_bytes and b"orchard" not in audit_bytes

    # One file changes: only its new chunks are put, then its vanished ones deleted.
    (root / "b.md").write_text("# Beta\n\nbeta harbor rewritten notes\n", encoding="utf-8")
    update(store=store, watch_root=root, paths=[root / "b.md"], embedder=FakeEmbedder())
    after = _store_vectors(store)
    added, vanished = sorted(set(after) - set(vectors)), sorted(set(vectors) - set(after))
    assert added and vanished
    fake.calls.clear()
    second = _publish(store)
    assert (second.put, second.deleted, second.listed_remote) == (len(added), len(vanished), False)
    methods = fake.methods()
    assert "list_vectors" not in methods
    put_keys = [i["key"] for m, p in fake.calls if m == "put_vectors" for i in p["vectors"]]
    delete_keys = [k for m, p in fake.calls if m == "delete_vectors" for k in p["keys"]]
    assert (put_keys, delete_keys) == (added, vanished)
    assert max(i for i, m in enumerate(methods) if m == "put_vectors") < methods.index(
        "delete_vectors"
    )
    assert {key: value["data"] for key, value in _remote(fake).items()} == after

    # An unchanged store makes no put, delete or listing, and rewrites nothing.
    receipt_bytes = (store / "meta" / "vector-publication.json").read_bytes()
    fake.calls.clear()
    third = _publish(store)
    assert (third.put, third.deleted, third.unchanged) == (0, 0, len(after))
    assert fake.methods() == ["get_vector_bucket", "get_index"]
    assert (store / "meta" / "vector-publication.json").read_bytes() == receipt_bytes

    # A tampered key list is not trusted: one listing, the orphan deleted, nothing re-put.
    _remote(fake)["orphan"] = {"data": [0.0] * EMBED_DIM, "metadata": {}}
    with (store / "meta" / "vector-publication-keys.txt").open("a") as handle:
        handle.write("tampered\n")
    fake.calls.clear()
    fourth = _publish(store)
    assert (fourth.listed_remote, fourth.put, fourth.deleted) == (True, 0, 1)
    assert "orphan" not in _remote(fake)

    # No receipt, or an interrupted one, vouches for nothing: list and re-put every key.
    for damage in ("delete", "publishing"):
        path = store / "meta" / "vector-publication.json"
        if damage == "delete":
            path.unlink()
        else:
            path.write_text(path.read_text().replace('"complete"', '"publishing"'))
        again = _publish(store)
        assert (again.listed_remote, again.put, again.deleted) == (True, len(after), 0)

    # Text metadata republishes every key with its text, relative path and heading.
    with_text = _publish(store, text_metadata=True)
    assert with_text.put == len(after) and not with_text.listed_remote
    expected_metadata = _store_metadata(store)
    assert {k: v["metadata"] for k, v in _remote(fake).items()} == {
        key: expected_metadata[key] for key in after
    }
    assert _publish(store, text_metadata=True).put == 0

    # A failure midway through a metadata change leaves the receipt interrupted; the next
    # publish re-puts every key, so no vector keeps the old metadata.
    fake.failures["put_vectors"] = [None, client_error("ValidationException", "PutVectors")]
    with pytest.raises(IndexError, match="PutVectors failed: ValidationException") as failed:
        _publish(store)
    usage = failed.value.vector_usage  # type: ignore[attr-defined]
    assert usage.failed_requests == 1 and usage.operations["PutVectors"] == 2
    assert json.loads((store / "meta" / "vector-publication.json").read_text())["state"] == (
        "publishing"
    )
    recovered = _publish(store)
    assert recovered.listed_remote and recovered.put == len(after)
    assert all(value["metadata"] == {} for value in _remote(fake).values())

    # A throttled put is retried by palace; both attempts are audited and counted.
    (root / "c.md").write_text("# Gamma\n\ngamma quarry changed\n", encoding="utf-8")
    update(store=store, watch_root=root, paths=[root / "c.md"], embedder=FakeEmbedder())
    fake.failures["put_vectors"] = [client_error("TooManyRequestsException", "PutVectors")]
    before = len(_audit(store))
    retried = _publish(store)
    attempts = [r for r in _audit(store)[before:] if r["operation"] == "PutVectors"]
    assert [(r["status"], r["attempt"]) for r in attempts[:2]] == [
        ("TooManyRequestsException", 1),
        ("ok", 2),
    ]
    assert retried.usage.failed_requests == 1 and sleeps == [s3v.BACKOFF_BASE_SECONDS]
    assert retried.usage.request_bytes == sum(r["request_bytes"] for r in _audit(store)[before:])

    # Another embedding identity: the receipt no longer vouches, so every key is replaced,
    # also after an interruption, and the remote vectors end in the new space.
    save_embedder_config(
        store,
        EmbedderConfig(
            provider="openrouter",
            model="fixture/other-model",
            upstream="FixtureUpstream",
            dim=EMBED_DIM,
            published_corpus=True,
            published_corpus_note="fixture",
            published_corpus_asserted_at="2026-09-28T12:00:00-06:00",
        ),
    )
    build(store=store, watch_root_filter=root, full=True, embedder=Negated())
    fake.failures["put_vectors"] = [None, client_error("InternalServerException", "PutVectors")]
    fake.failures["put_vectors"] += [client_error("InternalServerException", "PutVectors")] * 3
    with pytest.raises(IndexError, match="InternalServerException"):
        _publish(store)
    replaced = _publish(store)
    assert replaced.put == len(_store_vectors(store))
    assert {k: v["data"] for k, v in _remote(fake).items()} == _store_vectors(store)

    # Stale keys on a later listing page and a deletion that fails part-way: the receipt
    # stays interrupted and the next publish deletes exactly what the store lacks.
    for index in range(3):
        _remote(fake)[f"stale-{index}"] = {"data": [0.0] * EMBED_DIM, "metadata": {}}
    fake.list_page_size = 2
    (store / "meta" / "vector-publication.json").unlink()
    monkeypatch.setattr(s3v, "MAX_VECTORS_PER_CALL", 1)
    fake.failures["delete_vectors"] = [None, client_error("ValidationException", "DeleteVectors")]
    with pytest.raises(IndexError, match="DeleteVectors failed"):
        _publish(store)
    assert json.loads((store / "meta" / "vector-publication.json").read_text())["state"] == (
        "publishing"
    )
    fake.calls.clear()
    healed = _publish(store)
    assert fake.methods().count("list_vectors") > 1 and healed.deleted == 2
    assert set(_remote(fake)) == set(_store_vectors(store))
    monkeypatch.setattr(s3v, "MAX_VECTORS_PER_CALL", 500)

    # A receipt write that fails after the remote change is an error with usage attached,
    # not a traceback; the interrupted receipt makes the next publish converge.
    (root / "a.md").write_text("# Alpha\n\nalpha orchard changed\n", encoding="utf-8")
    update(store=store, watch_root=root, paths=[root / "a.md"], embedder=Negated())
    real_write = s3v._atomic_write

    def failing_write(path: Path, data: bytes) -> None:
        if path.name == "vector-publication-keys.txt":
            raise OSError("fixture disk full")
        real_write(path, data)

    monkeypatch.setattr(s3v, "_atomic_write", failing_write)
    with pytest.raises(IndexError, match="fixture disk full") as write_failed:
        _publish(store)
    assert write_failed.value.vector_usage.operations["PutVectors"] >= 1  # type: ignore[attr-defined]
    monkeypatch.setattr(s3v, "_atomic_write", real_write)
    assert _publish(store).listed_remote
    assert {k: v["data"] for k, v in _remote(fake).items()} == _store_vectors(store)

    # Through the real SDK client, every HTTP attempt is palace's: SDK retries are off, so
    # each transport attempt appears exactly once in the audit, retries included.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    real_client = REAL_OPEN_CLIENT(None, REGION)
    sends: list[str] = []

    def unavailable(request: Any, **_: Any) -> Any:
        from botocore.awsrequest import AWSResponse

        class Raw:
            def stream(self, **_: Any) -> Any:
                yield b'{"message": "fixture unavailable"}'

        sends.append(request.url)
        headers = {"x-amzn-ErrorType": "ServiceUnavailableException"}
        return AWSResponse(request.url, 503, headers, Raw())

    real_client.meta.events.register("before-send.s3vectors", unavailable)
    monkeypatch.setattr(s3v, "open_client", lambda profile, region: real_client)
    before = len(_audit(store))
    with pytest.raises(IndexError, match="GetVectorBucket failed: ServiceUnavailableException"):
        _publish(store)
    attempts = _audit(store)[before:]
    assert len(sends) == len(attempts) == s3v.MAX_ATTEMPTS
    assert [r["attempt"] for r in attempts] == list(range(1, s3v.MAX_ATTEMPTS + 1))
    monkeypatch.setattr(s3v, "open_client", open_fake)

    # The command line: one JSON object on stdout, success or failure.
    capsys.readouterr()
    argv = ["index", "publish-vectors", "--store", str(store), "--bucket", BUCKET]
    argv += ["--index", INDEX, "--profile", "cli", "--region", REGION, "--json"]
    assert palace_main(argv) == 0
    out = capsys.readouterr()
    success = json.loads(out.out)
    assert success["ok"] is True and success["put"] == 0 and success["usage"]["requests"] == 2
    assert "palace index publish-vectors: put=0" in out.err and opened[-1] == ("cli", REGION)
    opt_in.unlink()
    assert palace_main(argv) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["ok"] is False and "not opted in" in failure["error"]
    assert failure["usage"]["requests"] == 0
