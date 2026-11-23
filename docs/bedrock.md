# Bedrock embedding and composed settings

As of 2026-11-07. Palace can embed a store with Cohere Embed v4 through AWS Bedrock, selected either from a Palace-owned `palace` section of a composed [Conric](https://github.com/wolfmcnally/conric) configuration or from the store's TOML selector file. Both routes go through one validator and one schema. Bedrock is third-party inference: it is never selected implicitly and is available only for an already-published corpus. As of 2026-11-07, two bounded runs against the real model in one Region with a synthetic ten-note corpus passed, one from a workstation profile and one from an EC2 instance role in a private subnet; other Regions, throughput and real-corpus behaviour are not established, and every repository proof is offline.

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

- **Model and Region.** The model id is the direct regional id `cohere.embed-v4:0`, invoked on the selector Region's standard `bedrock-runtime` endpoint. Inference profiles are refused because a profile may process a request in another Region than the one the store's identity names. As of 2026-11-07 the Embed v4 model card lists direct in-Region access only in `us-east-1`, `eu-west-1` and `ap-northeast-1` (AWS Bedrock user guide, `model-card-cohere-embed-v4.html`, retrieved 2026-11-07). Palace does not hard-code that list; an unavailable Region surfaces as the service's own refusal before any row is written.
- **Endpoints.** The runtime client ignores SDK endpoint-URL overrides (`AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` and a profile's endpoint setting), so every request goes to the Region the identity names. The provider sends only `InvokeModel` requests; it makes no Bedrock control-plane request.
- **Credentials.** What the AWS SDK resolves, nothing else: SigV4 credentials from its chain (an instance role, the named `profile` or the environment), or a Bedrock API key in the SDK's `AWS_BEARER_TOKEN_BEDROCK` environment variable. The selector never carries a secret. With neither, the provider refuses before any request: `bedrock credentials unavailable: configure the AWS SDK credential chain (profile, environment or instance role) or a Bedrock API key in AWS_BEARER_TOKEN_BEDROCK`. A set-but-blank token variable, or a token outside the RFC 6750 bearer-token syntax (letters, digits, `-._~+/`, trailing `=`; no whitespace or newline), also refuses before any request, without showing the value. Expired or denied credentials surface as the service's error code alone: Bedrock failure messages are Palace's own text naming the operation, Region and a declared service error code, never the service's message, so neither a token nor any other service-supplied text appears in an error or the audit.
- **Bedrock API keys.** When the token variable is set, the SDK signs every `InvokeModel` request with `Authorization: Bearer`, and it prefers the token over SigV4 credentials when both are present; to use SigV4, unset the variable. Signing with a token is not authorization: the service decides whether that key may invoke the model. As of 2026-11-11 AWS documents Bedrock API keys as limited to Bedrock and Bedrock Runtime actions, excluding bidirectional streaming, Agents and Data Automation (AWS Bedrock user guide, `api-keys-use.html`, retrieved 2026-11-11). Short-term keys inherit their issuing principal's permissions; long-term keys carry their IAM user's policies. Palace's proofs of this path are offline: real SDK signing with synthetic values, not a live key.
- **IAM.** The administrator grants `bedrock:InvokeModel` to whichever principal authenticates (the role, profile or API key's identity) and accepts any marketplace agreement. Palace needs no other permission. These are administrator actions outside Palace.
- **Data retention is the account's responsibility.** Palace does not read, verify or enforce the account's Bedrock data-retention mode and makes no zero-data-retention promise. Configuring retention, and any assurance about what the service retains, rests with AWS and the account's administrator. Palace's declared boto3/botocore minimum is 1.43.90, the earliest release verified to honor the per-request read timeout that bounds a request's socket reads by an enclosing deadline.
- **Input types.** Documents are embedded with `input_type` `search_document` and queries with `search_query`. Each request body is exactly `texts`, `input_type`, `embedding_types: ["float"]`, `truncate: "NONE"` and `output_dimension` equal to the selector's `dim`. Both documented response shapes (an array of float arrays, or an object whose `float` key holds them) are accepted; any other shape, a count or dimension mismatch, or a non-finite value refuses before anything is written.
- **Bounds.** At most 96 inputs per request and a conservative aggregate budget of 120,000 request units, counted as each input's larger raw or NFC UTF-8 byte length plus eight, which stays below the model's documented 128K-token context. A single input above the budget is refused before any request. Build concurrency defaults to at most 4 callers, an unmeasured ceiling that `--embed-concurrency` can lower. The budget is a per-request size cap in bytes; it is not a token count and not a per-minute token or request quota, and no such quota is known or assumed. Throttling, unavailability, internal, model-timeout, model-not-ready and connection failures are retried under Palace's `RetryPolicy` (by default up to four attempts, jittered exponential backoff capped at 30 seconds, and a service `Retry-After` honored as a floor; a hint beyond the cap ends the request at once rather than retrying early); every other error refuses at once. SDK retries are disabled, so every attempt is Palace's. A failure raises a typed `ProviderCallError` whose `delivery` says whether any attempt may have been processed (see the index README). Inside an enclosing `call_scope`, no attempt starts after its deadline and each socket read of a request waits no longer than the time remaining when that request began, set per request without changing the shared client; connect and read timeouts are otherwise 60 seconds. That is a per-read bound, not a total-request bound: a response that keeps arriving in pieces can outlast the deadline, and SDK credential resolution is not bounded. A caller-supplied `RequestPacer` is consulted before every attempt.
- **Audit.** Every `InvokeModel` attempt appends a `cloud_embed_call` record to `<store>/events/cloud-egress/`, with the provider, Region, attempt, status and input digest. No corpus text, query text or credential is written.
- **Boundary.** The store must carry a complete published-corpus assertion (`published_corpus`, `published_corpus_note`, `published_corpus_asserted_at`). The default personal store and any watch root overlapping the personal vault are refused unconditionally. A Bedrock failure is a refusal, never a substitution of another provider.
- **Probe.** `probe()` resolves credentials and opens the runtime client; it sends no request. Credentials, permissions, model availability and the returned dimension are first exercised by the first real batch.
- **Daemon.** `palace index serve` refuses Bedrock stores; Bedrock embedding is synchronous-build-only.

## Identity and vector width

A Bedrock store's identity stamp is provider `bedrock:<region>`, model `cohere.embed-v4:0`, the selector's dimension and the shared text convention. The Region is part of the provider string because Palace has not measured whether the same model returns identical vectors across Regions. Equal dimensions never make vectors from different identities interchangeable.

Each store's vector table is declared at its selected dimension. If the selector's `dim` differs from the width an existing table was declared at, `palace index build` refuses before any request with the remedy of moving the chunks database aside and rebuilding from an empty one with `palace index build --full`. Default local stores keep their 4096-wide table unchanged.
