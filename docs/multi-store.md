# Explicit multi-store retrieval

`palace.multistore.search_stores(query=..., stores=[...])` searches a declared set of stores and returns `StoreHit` values with a canonical `store`, the existing `SearchHit` in `hit`, and an `origin_id` pair. Local chunk identifiers can collide across stores; origin pairs cannot. Store argument order does not affect ties. Duplicate paths (including aliases), missing databases and incompatible embedding identities refuse the whole request. Database reads do not create directories.

Each vector query uses the store's recorded embedding convention, model, provider and dimension. Matching identities share one invocation-local vector cache; different identities require separate embeddings. Every configured remote boundary is checked before any provider is constructed. A real shared remote request is audited in the first canonical store for that identity, which owns the constructed provider. No provider is substituted. Injected providers remain caller-owned; constructed clients and database snapshots close on success or failure.

Each store runs the existing retrieval and expansion pipeline. Cross-store fusion uses per-store ranks rather than incomparable raw vector or BM25 scores. The candidate union is hydrated and reranked once, with store-qualified identifiers preserving origin through ties and collisions. Retrieval defaults, pool sizes, models and expansion settings are unchanged. A missing reranker is a strict error in both the multi-store library and CLI; `strict=False` or `--allow-rerank-failure` explicitly permits fused results with `failed` status. `--no-rerank` reports `disabled`. A request with one CLI store retains the existing single-store behavior.

## Production invocation

Repeat `--store` to request multiple stores. Each must already contain a compatible index and have its required inference provider available.

```bash
palace search --store /path/to/store-a --store /path/to/store-b "retention policy"
```

Use `--json` for JSONL with `store`, `origin_id`, local hit fields and `rerank_status`. Human output includes the store on each result. Missing stores and strict failures exit nonzero without partial results. Zero-hit JSONL is empty; the library still returns its status. A zero-hit `applied` status does not prove reranker health because no inference is needed.

## Synthetic walkthrough

Run these commands from the repository. The driver builds six fixture documents, uses token-hashed embedding vectors and a lexical reranker, and routes search through the production CLI. It needs no model download, service, paid call or real corpus. These vectors are fake: query these stores only through the driver. This demonstrates retrieval plumbing and origin, not learned-model quality. The operator's perceptual acceptance remains open after the automated checks.

Create the marked fixture directory; an existing `.demo` refuses without overwriting it:

```bash
./bin/palace-multistore-demo setup
```

Search both stores and inspect the origin of each retention result:

```bash
./bin/palace-multistore-demo search --store .demo/store-a --store .demo/store-b "retention policy"
```

Reverse the store order; the result order should remain identical:

```bash
./bin/palace-multistore-demo search --store .demo/store-b --store .demo/store-a "retention policy"
```

Remove one store from the request; only that store's documents should remain:

```bash
./bin/palace-multistore-demo search --store .demo/store-a "retention policy"
```

Request a missing store; the command should refuse the whole search and leave the path absent:

```bash
./bin/palace-multistore-demo search --store .demo/store-a --store .demo/missing "retention policy"
```

Inspect unrelated results with scores. Vector retrieval may still return candidates; zero lexical scores are not semantic relevance, and this phase adds no relevance threshold:

```bash
./bin/palace-multistore-demo search --store .demo/store-a --store .demo/store-b --verbose "lunar geology"
```

Inspect exact origin pairs in machine output:

```bash
./bin/palace-multistore-demo search --store .demo/store-a --store .demo/store-b --json "retention policy"
```

Cleanup verifies the marked inventory and refuses to remove changed or unexpected content:

```bash
./bin/palace-multistore-demo cleanup
```

Each query prints its measured in-process duration to stderr. Those synthetic measurements exclude startup, real inference, network latency and production corpus size; they are not production latency estimates. The retained search tests separately prove shared and different embedding identities, collision-safe origin, rank-only fusion, one rerank of the full union, strict/explicitly degraded failure, and nondestructive refusal. Platform qualification is described in [portability](portability.md).

As of 2026-09-06, the six-document macOS ARM64 fixture returned retention-related text in all top-three combined results, with both store origins represented. Reversing store order produced identical JSONL. Five repetitions measured median in-process times of 6.875 ms for one store and 9.708 ms for both with the same stub providers. The unrelated query produced zero lexical rerank scores. These observations validate fixture behavior, not semantic quality or a production speed advantage. Missing/duplicate/outside-demo store requests refused, and cleanup preserved an intentionally added unexpected file before succeeding after that owned test file was removed. Disposable fixtures use rollback journals because read-only WAL connections can create sidecar files; cleanup checks the complete unchanged fixture inventory.
