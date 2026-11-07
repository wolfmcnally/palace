# Bedrock embedding and composed settings

As of 2026-11-07. Palace can embed a store with Cohere Embed v4 through AWS Bedrock, selected either from a Palace-owned `palace` section of a composed [Conric](https://github.com/wolfmcnally/conric) configuration or from the store's TOML selector file. Both routes go through one validator and one schema. Bedrock is third-party inference: it is never selected implicitly and is available only for an already-published corpus. As of 2026-11-07, one bounded workstation-profile run against the real model in one Region with a synthetic ten-note corpus passed; instance-role authentication, other Regions, throughput and real-corpus behaviour are not established, and every repository proof is offline.

## The `palace` section

A consumer that composes its configuration from several YAML files hands Palace those files and the root it loaded them with. Palace reads only its own top-level `palace` mapping, whose only key is `embedding`:

```yaml
palace:
  embedding:
    provider: bedrock
    model: cohere.embed-v4:0
    region: us-east-1
    dim: 1536
    # optional:
    profile: analytics
    published_corpus: true
    published_corpus_note: Published at https://example.test/docs/
    published_corpus_asserted_at: "2026-11-07T00:00:00Z"
```

Files compose in order, and later files override earlier keys, so a shared defaults file can carry the provider, model and Region while a site file supplies the dimension. From the command line:

```bash
palace providers set --operation embedding --store <store> --settings defaults.yaml --settings site.yaml
```

From Python, `palace.settings.load_palace_settings(paths, root=root)` returns the validated settings and `palace.settings.initialize_store(store, settings)` writes the store's selector. Neither makes an inference request, constructs a client or reads a credential. Composed settings select only embedding; `--settings` with `--operation reranking` is refused.

### Once-only initialization

Initialization writes the store's selector once. If the store already has a selector equal to the composed one, it is left byte-for-byte alone and the command prints `Embedding selector unchanged; no inference request was made.` A different existing selector is refused rather than overwritten, because the selector is the store's recorded identity source. To change a store's embedding, move its chunks database and `meta/embedder.toml` aside, initialize again, then run `palace index build --full`. (`palace providers set --config` keeps its explicit replace semantics.)

### The equivalent TOML selector

`<store>/meta/embedder.toml` carries the same keys at top level:

```toml
provider = "bedrock"
model = "cohere.embed-v4:0"
region = "us-east-1"
dim = 1536
published_corpus = true
published_corpus_note = "Published at https://example.test/docs/"
published_corpus_asserted_at = "2026-11-07T00:00:00Z"
```

### Refusals

Each refusal names the field's path and, for composed settings, the file, line and column Conric attributes. No configured value is echoed. The same text appears on both routes:

| Condition | Text |
|---|---|
| A key under `palace` other than `embedding`, or a field the provider does not accept | `unknown field; remove it` |
| A required field (`provider`, `model`, `region`, `dim`) is absent | `required field is missing` |
| A provider other than `ollama`, `openrouter`, `endpoint` or `bedrock` | `provider must be one of ollama, openrouter, endpoint or bedrock` |
| An inference-profile model id (`us.`, `eu.`, `global.` prefixes or an ARN) | `inference-profile model ids are refused; use the direct regional model id` |
| Any other model | `model must be one of cohere.embed-v4:0` |
| A Region that is not an AWS Region name | `region must be an AWS Region name such as us-east-1` |
| A dimension outside the model's options | `dim must be one of 256, 512, 1024 or 1536` |
| A non-boolean `published_corpus`, or an empty string field | `must be true or false` / `must be a non-empty string` |
| An `!env` reference anywhere but `endpoint.credential_env` | `an !env reference is accepted only at endpoint.credential_env` |

A composed refusal reads, for example, `palace settings /path/site.yaml:3:10: palace.embedding.dim: dim must be one of 256, 512, 1024 or 1536`. A missing field is attributed to the nearest enclosing mapping, which is the last file that defines `palace.embedding`.

## The `bedrock` provider

- **Model and Region.** The model id is the direct regional id `cohere.embed-v4:0`, invoked on the selector Region's standard `bedrock-runtime` endpoint. Inference profiles are refused because a profile may process a request in another Region, which would detach the retention check from the request. As of 2026-11-07 the Embed v4 model card lists direct in-Region access only in `us-east-1`, `eu-west-1` and `ap-northeast-1` (AWS Bedrock user guide, `model-card-cohere-embed-v4.html`, retrieved 2026-11-07). Palace does not hard-code that list; an unavailable Region surfaces as the service's own refusal before any row is written.
- **Endpoints.** Both the control-plane client and the runtime client ignore SDK endpoint-URL overrides (`AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_BEDROCK`, `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` and a profile's endpoint setting), so the retention read and every request go to the Region the identity names.
- **Credentials.** The AWS SDK credential chain only: an instance role, the named `profile` or the environment. The selector never carries a secret. With no resolvable credentials, the provider refuses before any request: `bedrock credentials unavailable: configure the AWS SDK credential chain (profile, environment or instance role)`. Expired or denied credentials surface as the service's error code.
- **IAM.** The administrator grants `bedrock:InvokeModel` and `bedrock:GetAccountDataRetention`, accepts any marketplace agreement, and configures the Region's retention mode. These are administrator actions outside Palace.
- **Retention check, fail closed.** Before the first embedding request, each provider instance reads `GetAccountDataRetention` on the Bedrock control plane in the selector's Region and refuses unless the mode is exactly `none`, for example `bedrock retention mode in us-east-1 is 'inherit'; palace requires 'none'`. **What the check means:** it reads one Region's configured mode at one moment. It does not observe what the service retains, cannot see what a cross-region profile does in its destination Regions, does not read any per-model override, and is not a substitute for the account administrator's configuration or a compliance attestation.
- **Input types.** Documents are embedded with `input_type` `search_document` and queries with `search_query`. Each request body is exactly `texts`, `input_type`, `embedding_types: ["float"]`, `truncate: "NONE"` and `output_dimension` equal to the selector's `dim`. Both documented response shapes (an array of float arrays, or an object whose `float` key holds them) are accepted; any other shape, a count or dimension mismatch, or a non-finite value refuses before anything is written.
- **Bounds.** At most 96 inputs per request and a conservative aggregate budget of 120,000 request units, counted as each input's larger raw or NFC UTF-8 byte length plus eight, which stays below the model's documented 128K-token context. A single input above the budget is refused before any request. Build concurrency defaults to at most 4 callers, an unmeasured ceiling that `--embed-concurrency` can lower. Throttling, unavailability, internal, model-timeout, model-not-ready and connection failures are retried up to four attempts with exponential backoff; every other error refuses at once. SDK retries are disabled, so every attempt is Palace's.
- **Audit.** Every retention read appends a `bedrock_retention_check` record and every `InvokeModel` attempt appends a `cloud_embed_call` record to `<store>/events/cloud-egress/`, with the provider, Region, attempt, status and input digest. No corpus text, query text or credential is written.
- **Boundary.** The store must carry a complete published-corpus assertion (`published_corpus`, `published_corpus_note`, `published_corpus_asserted_at`). The default personal store and any watch root overlapping the personal vault are refused unconditionally. A Bedrock failure is a refusal, never a substitution of another provider.
- **Probe.** `probe()` is the authenticated retention read, not an embedding. Model availability and the returned dimension are first observed on the first real batch.
- **Daemon.** `palace index serve` refuses Bedrock stores; Bedrock embedding is synchronous-build-only.

## Identity and vector width

A Bedrock store's identity stamp is provider `bedrock:<region>`, model `cohere.embed-v4:0`, the selector's dimension and the shared text convention. The Region is part of the provider string because Palace has not measured whether the same model returns identical vectors across Regions. Equal dimensions never make vectors from different identities interchangeable.

Each store's vector table is declared at its selected dimension. If the selector's `dim` differs from the width an existing table was declared at, `palace index build` refuses before any request with the remedy of moving the chunks database aside and rebuilding from an empty one with `palace index build --full`. Default local stores keep their 4096-wide table unchanged.
