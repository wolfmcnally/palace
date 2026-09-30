# Private inference providers

Palace supports explicitly configured HTTPS embedding and reranking endpoints for deployed stores inside an operator-controlled boundary. Local Ollama embeddings and the local reranker remain the defaults. Configuration is an operator attestation, not proof that a hostname is private: verify the service, infrastructure, retention and access controls before authorizing real content. The default personal store and any watch root overlapping the personal vault are refused even with that attestation. Existing third-party OpenRouter embedding still requires the separate published-corpus opt-in.

## Selectors and identity

`palace providers list` lists supported operation/provider kinds. `palace providers set --operation embedding|reranking --store PATH --config FILE` validates and atomically installs one TOML selector without making an inference request. Embeddings use `meta/embedder.toml`; reranking uses `meta/reranker.toml`. Credentials are environment-variable references, never secret values in TOML. Missing credentials or invalid CA material refuse before inference. Changing embedding identity requires a full index rebuild; queries refuse until the recorded identity matches the selected configuration.

An endpoint embedding selector has this shape; reranking omits `dim` and supplies its own model:

```toml
provider = "endpoint"
model = "deployment-model-id"
dim = 4096

[endpoint]
name = "deployment-inference"
url = "https://inference.example.invalid/inference"
credential_env = "PRIVATE_INFERENCE_TOKEN"
adapter = "palace-json-v1"
authorized_boundary = "operator-controlled"
boundary_note = "Describe the approved deployment boundary and its custody controls."
boundary_asserted_at = "2026-09-06T00:00:00Z"
timeout_seconds = 30
max_attempts = 2
# ca_bundle = "/absolute/path/to/trusted-ca.pem"
```

Use the actual authorization timestamp. Unknown endpoint fields, URL credentials, query strings, fragments, HTTP, unsupported adapters, absent attestation and invalid bounds refuse. The optional CA bundle augments the explicit trust configuration; verification is never disabled. Ambient HTTP proxies and redirects are disabled. The provider identity combines the name with a digest of the exact URL, adapter and credential-variable reference; model, dimension and embedding convention retain their own identity axes. Rotating the value behind the same credential reference preserves identity. Changing the reference changes identity because it can select another deployment account.

Missing reranker configuration selects the current local model. `provider = "local"` is the complete explicit local reranker selector. Both single-store and multi-store vector retrieval construct the selected embedding provider. There is no local substitution for a remote-built vector space. Multi-store queries require one matching configured reranker identity across stores before any provider request, then rerank the union once. Shared embedding requests are audited in the first canonical store for that identity; the union rerank is audited in the first canonical selected store. Library outcomes and JSONL hits expose `reranker_identity`; an injected caller-owned reranker has unspecified identity (`null`).

## Native wire contract

`palace-json-v1` is Palace's explicit adapter protocol. A deployment must implement it; this is not a claim of vendor API compatibility. Requests are HTTPS POST with `Authorization: Bearer <referenced value>` and JSON bodies:

```json
{"operation":"embedding","model":"deployment-model-id","input":["first text","second text"],"dim":4096}
```

```json
{"operation":"reranking","model":"rerank-model-id","query":"question","documents":["first passage","second passage"]}
```

Replies echo the configured endpoint `name` as `provider` and the exact requested `model`. Each `data` item has a unique integer `index` addressing its input and either an `embedding` array of the configured dimension or one numeric `relevance_score`. Indices must cover every input exactly once; response order may differ. Booleans, strings, non-finite numbers, partial replies and mismatched identity refuse. Embedding batches contain at most 128 inputs with one request in flight. Responses are capped at 16 MiB. Timeouts are positive and at most 120 seconds; attempts are integers from one to three. Only transport failures, HTTP 429 and HTTP 5xx retry, with bounded backoff. The timeout applies to transport operations and is also checked while consuming response chunks; it is not an operating-system hard deadline for the whole search.

Each attempt appends an audit record before transmission and a response record afterward under `events/cloud-egress/`. They share a request identifier and attempt number and include operation, configured model/provider, ordered input digest/count, HTTP status and elapsed time. Reranking input digests cover the query followed by documents. Corpus text, credential values, endpoint URLs and upstream error bodies are omitted. A response record describes the HTTP attempt, not semantic validation success: HTTP 200 can still be followed by a schema or identity refusal. A failed pre-request append prevents transmission; a failed response append refuses the operation and stops retries. Token usage and cost remain unknown when the adapter supplies none.

Private reranking defaults to strict failure in the library and CLI. Explicit `strict=False` or `--allow-rerank-failure` permits fused results only for operational unavailability and marks them `failed`; authentication, identity, schema and audit errors remain hard failures. `--no-rerank` reports `disabled` and makes no reranking request. Local single-store CLI behavior remains lenient. Empty results do not prove provider health.

## Synthetic HTTPS walkthrough

From the repository, inspect the two public entry points:

```bash
./bin/python -m palace.cli index build --help
```

```bash
./bin/python -m palace.cli search --help
```

Create a disposable fixture, self-signed one-day certificate and explicit selectors. An existing `.private-demo` refuses. This uses synthetic lexical models, not learned-model quality or a production performance test; OpenSSL must be available.

```bash
./bin/palace-private-demo setup
```

In a second terminal, run the local server in the foreground:

```bash
./bin/palace-private-demo serve
```

In the first terminal, configure both operations:

```bash
./bin/python -m palace.cli providers set --operation embedding --store .private-demo/store --config .private-demo/embedding.toml
```

```bash
./bin/python -m palace.cli providers set --operation reranking --store .private-demo/store --config .private-demo/reranking.toml
```

Build and search with the fixture-only credential:

```bash
PALACE_PRIVATE_DEMO_TOKEN=synthetic-private-demo-only ./bin/python -m palace.cli index build --store .private-demo/store --watch-root .private-demo/source
```

```bash
PALACE_PRIVATE_DEMO_TOKEN=synthetic-private-demo-only ./bin/python -m palace.cli search --store .private-demo/store --json "retention policy"
```

Inspect `.private-demo/store/events/cloud-egress/`: the records should identify both operations without the credential or document text. In `.private-demo/embedding.toml`, temporarily remove `authorized_boundary` and repeat its configuration command: it must refuse without replacing the installed selector. Restore the line. Temporarily change `model`, install it and repeat search: it must refuse the stored embedding identity before transmission. Restore the original selector and install it again.

Stop the server with Ctrl-C, then repeat search: it must refuse without constructing a substitute provider. To isolate reranker unavailability, use lexical retrieval with the same credential:

```bash
PALACE_PRIVATE_DEMO_TOKEN=synthetic-private-demo-only ./bin/python -m palace.cli search --store .private-demo/store --mode bm25 --allow-rerank-failure --json "retention policy"
```

This explicitly degraded request should return `failed` status; omitting the lenience flag should refuse. Cleanup checks the marked fixture, refuses while the server port is occupied, and refuses changed source/certificate bytes, symlinks or unexpected files:

```bash
./bin/palace-private-demo cleanup
```

Operator judgment of the walkthrough and diagnostic clarity remains separate from automated qualification. No real endpoint provisioning, private corpus, paid call, Bedrock adapter, remote rerank vendor or Linux daemon is included.
