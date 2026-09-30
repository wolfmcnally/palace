import hashlib
import math
import sqlite3
import sys
import tempfile
from pathlib import Path

import palace
from palace.index._errors import IndexError
from palace.index.build import build
from palace.index.config import EMBED_DIM, chunks_db_path
from palace.index.embedder_config import resolve_identity
from palace.multistore import search_stores
from palace.search import search
from palace.watch.config import WatchRootsConfig, default_config_path

assert palace.__version__ == "0.1.0"

assert Path(palace.__file__).is_relative_to(Path(sys.prefix)), palace.__file__


class SyntheticEmbedder:
    dim = EMBED_DIM
    max_concurrency = 1

    def probe(self):
        pass

    def close(self):
        pass

    def embed(self, texts):
        result = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            v = [(h[i % 32] - 128) / 128 for i in range(self.dim)]
            n = math.sqrt(sum(x * x for x in v))
            result.append([x / n for x in v])
        return result


with tempfile.TemporaryDirectory(prefix="palace-wheel-smoke-") as tmp:
    root = Path(tmp)
    stores = []
    e = SyntheticEmbedder()
    for i in range(2):
        store = root / f"store{i}"
        source = root / f"source{i}"
        source.mkdir()
        (source / "notes.md").write_text(f"# Observatory\n\nCalibration checkpoint {i}.")
        cfg = WatchRootsConfig()
        cfg = cfg.with_added(source, store_root=store)
        cfg.save(default_config_path(store))
        r = build(store=store, embedder=e, log=lambda _: None)
        assert r.roots[0].embedded == 1
        assert search(query="Calibration", store=store, mode="hybrid", embedder=e)
        stores.append(store)
    identities = {resolve_identity(s): e for s in stores}
    merged = search_stores(
        query="Calibration", stores=stores, embedders=identities, rerank_enabled=False
    )
    assert len(merged.hits) >= 2
    assert {h.store for h in merged.hits} == {s.resolve() for s in stores}
    with sqlite3.connect(chunks_db_path(stores[0])) as conn:
        conn.execute("UPDATE index_meta SET value='wrong' WHERE key='embed_model'")
        conn.commit()
    try:
        search(query="Calibration", store=stores[0], embedder=e)
    except IndexError:
        pass
    else:
        raise AssertionError("incompatible identity accepted")
    print(
        "PASS wheel0.1.0 isolated import, synthetic indexing/retrieval, "
        "multistore origin, incompatible identity refusal"
    )
