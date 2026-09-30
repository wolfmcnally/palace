# Retrieval

This policy fixes palace's read-time retrieval stack, its tunable knobs, and
the failure posture of the local reranker and opt-in query expansion. Every
tunable implementation value has one home: `palace/retrieval_config.py`.
Artifact identities and acquisition pins live in `palace/rerank_model.py`.

## 1. Stack of record

L1 retrieval is BM25 (SQLite FTS5 with porter stemming) plus vector ANN
(sqlite-vec over local `qwen3-embedding:8b` vectors), fused by Reciprocal Rank
Fusion with `k = 60`. When requested, the fourth stage sends the top fused
pool to a local cross-encoder and returns its highest-scoring survivors.

## 2. Reranker model and runtime

The reranker is `ms-marco-MiniLM-L-6-v2`, stored as palace runtime state at
`~/.local/share/palace/models/ms-marco-MiniLM-L-6-v2/` and run as an ONNX
graph by `onnxruntime` with `CPUExecutionProvider`. `XDG_DATA_HOME`, when set,
replaces `~/.local/share` only when it is an absolute path, as the XDG Base
Directory Specification requires; an empty or relative value is ignored.
`PALACE_MODELS_DIR` overrides the complete shared `models` directory.
`palace/retrieval_config.py::rerank_model_dir()` is the only implementation of
that precedence. A store never contains model artifacts.

The ownership contract is: **a store root plus palace's runtime is everything
a client needs**. The index-time embedder is selected explicitly per store:
local Ollama is the absent-config default, while an already-published corpus may opt into a pinned OpenRouter upstream. An explicitly authorized private deployment may select a native HTTPS embedding or reranking endpoint under the local-first policy. Local reranker artifacts remain shared runtime state; selection belongs to the store. Clients say what to index and how to search; palace owns model
construction and verifies the complete embedding identity recorded in the
portable index.

`tokenizers` performs pair encoding. Palace ranks by the model's raw logit:
the graph's default activation is identity, and a sigmoid would preserve order
while obscuring useful score spread.

The two artifacts are pinned:

| Artifact | Source | Bytes | SHA-256 |
|---|---|---:|---|
| `model.onnx` | `https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/resolve/main/onnx/model.onnx` | 91011230 | `5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a` |
| `tokenizer.json` | `https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/resolve/main/tokenizer.json` | 711396 | `d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66` |

`palace/rerank_model.py` is the single implementation of acquisition and owns
these pins. A reranked query verifies the artifacts and transparently fetches
anything absent, emitting one notice naming the target directory:

`palace search: fetching reranker model into <dir>`

A second query finds verified artifacts and emits no fetch notice. Existing
mismatching artifacts are refused rather than overwritten; only the explicit
`./bin/palace-rerank-model --force` operator surface replaces them. The same
tool supports deliberate prefetching and offline setup, but its `bin/` entry
point is only a repository-toolchain wrapper around the importable palace
implementation.

`sentence-transformers` and `torch` are rejected because their installation
and per-process import cost are disproportionate to a small CLI reranker.
`onnxruntime-silicon` is an unmaintained fork; the mainline arm64 wheel is the
runtime of record. NumPy is a documented transitive dependency of
`onnxruntime`; `palace/rerank.py` imports it directly because ONNX Runtime's
Python binding takes ndarray feeds.

### Future OpenRouter provider — not yet implemented in Phase 7.3

OpenRouter's rerank wire schema was confirmed against its first-party
[RAG evaluation cookbook](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/rag)
on 2026-08-08 from three agreeing samples. A request carries `model`, `query`,
`documents` as plain strings, and `top_n`. The response envelope is `results`;
each result carries `index`, `relevance_score`, and a `document.text` echo.
The following provider phase must pin `results` as the single accepted envelope
key rather than accepting speculative alternatives.

This paragraph documents a future provider; palace does **not** implement an
OpenRouter reranker in Phase 7.3. `usage`, `id`, and echo suppression remain
unconfirmed. An earlier observation of a `data` envelope is superseded because
it came from Inference proxy Chat, a third-party proxy, not OpenRouter.

## 3. Pair budget and truncation

`RERANK_MAX_TOKENS` = 128 (`palace/retrieval_config.py::RERANK_MAX_TOKENS`).
The cap covers the whole `[CLS] query [SEP] passage [SEP]` pair. A typical
eight-token question therefore leaves about 117 WordPiece tokens, roughly 460
characters, for the passage; a long section is scored on its opening.

The choice is measured, not assumed. Warm batch-20 `session.run` p50 on this
machine was 52.5 ms at padded sequence length 125, 103.7 ms at 191, and
275.3 ms at 512. The scoring tokenizer uses `only_second` truncation, so only
the passage can lose tokens. An oversized query is a caller error: palace
prints one `error:` line naming its token count and `RERANK_MAX_TOKENS`; it
never silently clips the query. Raising the cap requires a fresh
`./bin/palace-rerank-bench --assert-under-ms 100` run on the target machine.
The breadcrumb defined in §8 consumes part of this unchanged 128-token pair
budget; because it prefixes the passage, it survives `only_second` truncation
and deliberately trades passage tail for chunk identity. Measured breadcrumb
token cost: **TBD-from-bench**.

## 4. Latency budget and boundary

`RERANK_LATENCY_BUDGET_MS` = 100
(`palace/retrieval_config.py::RERANK_LATENCY_BUDGET_MS`). The budget covers the
complete warm `rerank()` boundary: `candidate_text` extraction, pair
tokenization, `session.run`, sorting, and truncation. It does not cover only
`session.run`. The benchmark reports `model_load_ms` and end-to-end wall clock
separately, and always prints the resolved store and model directory.
`model_load_ms` covers the complete `load_reranker()` boundary: full size and
SHA-256 verification of both artifacts, automatic acquisition when anything
is absent, tokenizer and ONNX session construction, and the loader probe
warm-up. On this machine, SHA-256 over the 91,011,230-byte `model.onnx` with
1 MiB chunks measured p50 **34.4 ms** (min 31.9 ms, max 41.5 ms). Palace
deliberately pays that per-query verification cost so digest-tamper refusal
holds on every reranked path; it is outside the 100 ms warm `rerank()` budget
but remains visible in end-to-end CLI latency.

## 5. Pool sizes

`rerank_in` = 20 (`palace/retrieval_config.py::RERANK_IN`).

`rerank_out` = 5 (`palace/retrieval_config.py::RERANK_OUT`).

The former is the fused pool scored by the cross-encoder. The latter is the
default output size when reranking is enabled; an explicit `--limit` remains
the sole output-size knob.

## 6. Default-on, with the evaluation debt named

`RERANK_ENABLED_DEFAULT` = True
(`palace/retrieval_config.py::RERANK_ENABLED_DEFAULT`). A bare `palace search`
therefore reranks and returns up to `rerank_out` = 5 results; `--no-rerank`
selects fused RRF and its ordinary default of 10 results.

The shipped default-on behavior carries an explicit relevance-evaluation debt. Representative evaluation must confirm or reverse it before further tuning. Synthetic fixtures alone do not satisfy that prerequisite. Risks include precise lexical queries and relevant text outside the pair-token prefix. Library defaults remain independent of this CLI choice.

## 7. Per-query expansion and routing

The L1 flags are `--rerank` / `--no-rerank`, `--rerank-in`, `--hyde`, and
`--multi-query [N]`. Expansion shapes retrieval and is independent of
reranking: both `--hyde --no-rerank` and `--multi-query --no-rerank` are valid.
Only `--hyde --mode bm25` is rejected, because BM25 has no vector leg to
replace, with this error:

`--hyde has no effect in --mode bm25 (HyDE replaces the vector leg's embedding input)`

`MULTI_QUERY_N` = 4 (`palace/retrieval_config.py::MULTI_QUERY_N`). This value
is only argparse's default for a bare `--multi-query`. `--multi-query N` uses
the caller's N exactly, N must be positive at the parser and library
boundaries, and the literal query always runs too: N rewrites produce N+1
fusion passes. HyDE replaces only each variant's vector embedding input; BM25
continues to receive the literal variant. Both expansion modes use the local
`qwen3.6:35b-a3b` Ollama model. There is no cloud path or cloud fallback.

## 8. Scoring text and embedding identity

`palace/index/enrich.py::scored_text(path, heading, body)` is the single
construction point for the text that represents a chunk to both the index-time
embedder and the cross-encoder. Its byte-exact output is:

```text
<path> > <heading>\n\n<body>
```

when `heading.strip()` is truthy, and:

```text
<path>\n\n<body>
```

when the heading is `None`, empty, or whitespace-only. The truthiness check
does not normalize the value: a present heading and the body are emitted
verbatim. For example, a Markdown section becomes
`notes/sample-project/caching.md > Decision\n\n<body>`. Markdown and AST-derived code
chunks normally use the path-and-heading form; JSONL, opaque text, and
line-window code chunks use the path-only form because they have no heading.

The path is the stored watch-root-relative POSIX path. That gives the model
stable file identity without embedding a machine-specific root and preserves
the relocatability of a shipped `chunks.sqlite`. The query embedding is never
breadcrumbed. The stored `chunks.body`, `body_hash`, `chunk_id`, FTS5 row,
snippets, and `expand` output remain byte-identical; BM25 deliberately does not
index the breadcrumb, because path or heading matches must not double-count
against body term frequency.

`EMBED_CONVENTION` = `breadcrumb-path-heading-v1`.

Every portable `chunks.sqlite` records a four-axis certificate in `index_meta`:

| key | Local example | Remote example |
|---|---|---|
| `embed_convention` | `breadcrumb-path-heading-v1` | `breadcrumb-path-heading-v1` |
| `embed_model` | `qwen3-embedding:8b` | `Qwen/Qwen3-Embedding-8B` |
| `embed_provider` | `ollama` | `openrouter:DeepInfra` |
| `embed_dim` | `4096` | `4096` |

The selector is `<store>/meta/embedder.toml`; absence means local Ollama. No
credential, environment variable, or build flag selects a provider. An OpenRouter
selector names the model, upstream, dimension, and complete published-corpus
assertion. `OPENROUTER_API_KEY` supplies credentials only after that selector
and the corpus boundary have selected and admitted the remote path. The loader
accepts exactly the keys named above and refuses any other outright, so a
selector carrying a stale or misspelled field name fails loudly rather than
silently reading as an unasserted corpus.

The OpenRouter pin was verified live on 2026-08-08. Every verification request
carried the production shape
`{"model": ..., "input": [...], "provider": {"order": ["<Upstream>"],
"allow_fallbacks": false, "zdr": true, "data_collection": "deny"}}`;
pinning `Nebius` and `DeepInfra` changed the
top-level response `provider` accordingly, while a nonexistent upstream
returned HTTP 404 rather than silently rerouting. Three upstreams served
`Qwen/Qwen3-Embedding-8B`: Nebius (32,000 context, first in the default order),
DeepInfra (32,768), and SiliconFlow (`siliconflow/fp8`, 32,768). Every
successful response must echo the configured upstream; divergence aborts
before vectors are returned. This response check is the only upstream-drift
detector and is never softened to a warning.

The identical-output invariant is venue-aware. Two local builds must remain
byte-identical. Two builds through the same remote configuration must have
byte-identical non-vector columns and vectors within
`EMBED_COSINE_TOLERANCE` = `1e-3`. Measurements on 2026-08-08 found
intra-upstream jitter `-2.2e-16 … 9.9e-05`, cross-upstream same-model distance
`5.4e-05 … 9.6e-05`, and local Ollama versus remote distance
`7.7e-03 … 1.19e-02` on the probe set. The committed fixture used a different
text and measured `2.1510e-02`, making the full observed venue-gap span
`7.7e-03 … 2.15e-02`. The tolerance is therefore roughly 10× above the jitter
it must accept and at least roughly 8× below the venue gap it must reject. It cannot
distinguish one remote upstream from another; the echoed-provider check owns
that provenance guarantee. Nebius happened to return repeat vectors
bit-identically while DeepInfra and SiliconFlow jittered, confirming that
determinism is an upstream property rather than a venue property.
The committed DeepInfra fixture capture on the same date measured repeat-call
distance `3.7956e-05`, placing both fixture pairs on the intended sides of the
same tolerance.

Every palace write path binds all four axes against the store selector.
Incremental `palace index build` refuses before walking any root; `--full` is
the only transition and re-stamps all four rows. Its final certificate remains
inside `BEGIN IMMEDIATE`, guarded by `PRAGMA data_version`, the concurrent
writer check, and the complete-root check. `palace index serve` parks before
constructing an embedder, writer, or event tail. `WriterWorker` independently
asserts the same tuple before entering its drain loop.

The reader binds all four identity axes to the selected store configuration before constructing its query embedder. Remote-built stores use their selected remote provider; no query assumes that another provider's vectors are interchangeable. The guard runs before query embedding or vector reads. Vector and hybrid retrieval, including reranked forms, are covered; pure lexical retrieval and section expansion do not require an embedding identity. Private reranking independently checks its authorization and the actual watch roots even for lexical retrieval. Both guards share this error shape:

`embedding-identity mismatch: <diverging axes> — rebuild it with 'palace index build --full'`

Write-time axes render as `model found='<value|none>' expected='<value>'` in
fixed convention/model/provider/dimension order. Reader-side missing rows
render as `model not recorded` or `provider not recorded`.

An identity mismatch does not make the launchd daemon fight its remedy.
Because the plist has `KeepAlive = true`, exiting would create a restart loop
that repeatedly competes with the full rebuild. The daemon instead publishes
`<store>/meta/index-daemon-state.json`, closes every SQLite connection, leaves
the cursor untouched, and reports `PARKED`. Every 30 seconds it reads the four
rows through a short-lived read-only connection. A completed full rebuild is a
transactional resume signal for an identity park: the daemon reports
`PARKED-REPAIRED` during the recheck window, logs `RESUMING`, then returns to
`ACTIVE`. A writer-readiness timeout is a different, typed park cause; it does
not use the identity recovery predicate or retry. Status prints its reason
loudly, and the operator corrects the cause and restarts the daemon. `palace
index status` uses the complete vocabulary `ACTIVE`, `PARKED`,
`PARKED-REPAIRED`, `STALE`, `STOPPED`, and `UNKNOWN`; corrupt state or failed
liveness checks never become a reassuring value.

Remote embedding is build-only and published-corpus-only — *published* in the
sense fixed by [`policies/local-first.md`](local-first.md) § "Public vs.
published": served from internet-reachable infrastructure, whether or not access
is gated. The build refuses the default personal store, any watch root
overlapping the personal vault, or a remote selector without the explicit
per-store published-corpus assertion before it constructs a client. An input list longer than `REMOTE_EMBED_MAX_BATCH` (1024,
the pinned upstream's own documented cap) is split into ordered batches, each
its own request and its own audit record carrying only that batch's chunk ids;
vectors are concatenated in input order. Every attempted cloud request,
including failures, startup probes, and operator-probe calls, appends a
content-addressed record to
`<store>/events/cloud-egress/YYYY-MM-DD.jsonl`. Records contain provider,
configured and observed upstream, model, dimension, watch root, exact-input
digest, chunk ids, prompt tokens, cost, HTTP status, and latency. The digest is
SHA-256 over canonical JSON bytes of the exact breadcrumbed input list; it is
not a chunk `body_hash`. This subdirectory is deliberately invisible to the
daemon's `events/YYYY-MM-DD.jsonl` tail.

`bin/palace-embed-remote-probe` is itself an audited cloud caller. Every mode
requires `--store`, refuses a store beneath the palace source root, and writes
one audit record per HTTP attempt. Its live pin, fixture-capture, and throughput
modes are intentionally outside `./bin/check all`; the automated suite uses
`httpx.MockTransport` and never reaches the network.

Retention was checked against primary provider documentation on 2026-08-08.
[OpenRouter's ZDR documentation](https://openrouter.ai/docs/guides/features/zdr)
states that OpenRouter does not retain prompts unless logging is explicitly
enabled and supports per-request `zdr: true`; its
[provider-routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection)
defines `data_collection: "deny"`. [DeepInfra's inference privacy policy](https://docs.deepinfra.com/account/data-privacy)
states that ordinary inference inputs and outputs remain only in memory,
are deleted after inference, and are not used for training (the listed Google
and Anthropic exceptions do not apply to Qwen). Palace applies both OpenRouter
request controls and pins DeepInfra, so zero retention and no training are the
operative settings; no human exception is required.

Remote-throughput changes require measurement against disposable, redistributable inputs. Preserve producer, writer and network attribution, repeated-sample intervals, actual attempt/audit counts, scale limits and unmet criteria. Use the topology-specific ceiling described in `briefs/remote-embedding-throughput.md`; do not treat an uncommitted private run as public qualification. Existing concurrency and retry limits remain binding in the implementation and provider policies.

## 9. Three-valued status, strict failure, and degradation taxonomy

Every result set has one rerank status: `applied`, `disabled`, or `failed`.
JSONL emits it as `rerank_status` on every hit. A zero-hit result emits no JSONL
record, so stderr and the library result remain the signal in that degenerate
case.

**An empty fused pool short-circuits to `applied` without loading the model.**
This is documented intent, not an accident of ordering: there is nothing to
rerank, so nothing can fail, and reporting `failed` would claim a failure that
did not occur. The consequence is stated plainly because it matters to anyone
building a health check: `applied` on a zero-hit query is **not** evidence that
the cross-encoder is loadable or functioning. A caller probing reranker health
must use a query that actually matches candidates, or call `load_reranker()`
directly. `palace.search.search_reranked()` returns the status and failure detail and
defaults to `strict=True`: an operational reranker failure raises
`RerankUnavailableError`. The single-store interactive CLI retains lenient local reranking, returning usable fused results with exit zero and one warning per process. Configured private reranking is strict unless the caller explicitly requests degradation; authentication, identity, schema and audit errors never degrade. The local warning is:

`palace search: rerank failed: <cause> — returning fused order (run ./bin/palace-rerank-model)`

Multi-store retrieval (`palace.multistore.search_stores` and repeated CLI `--store`) defaults to strict failure. `strict=False` or `--allow-rerank-failure` explicitly permits the same visible degraded result. The request validates every store before any provider work, shares query vectors only across identical embedding identities, fuses cross-store ranks rather than raw scores, and reranks the candidate union once. Every hit carries its canonical store and local chunk identifier. Duplicate or missing stores and identity/provider mismatches refuse the request; no store is silently omitted and no provider is substituted.

Private endpoint selectors, request/response validation and attempted-request audit follow [private providers](../docs/private-providers.md) and the local-first authorization boundary. Configured reranker identities must match across a multi-store request before any inference. JSONL and library results record the selected reranker provider/model; caller-injected identities remain unspecified. Adding this capability changes no ranking defaults or personal-evaluation prerequisite.

When reranking was opt-in, hard `IndexError` failures after verified-model
construction were appropriate. With reranking on by default, that posture
would hard-error every query on a corrupt installation. Loudness therefore
lives in the explicit status, preserved cause, warning, and strict library
default; the lenient CLI keeps the valid fused answer. Configuration and caller
errors remain errors because degrading them would misrepresent what was asked.

| Failure | Raised in | Class |
|---|---|---|
| Acquisition / verification failure (offline, incomplete download, digest mismatch) | `load_reranker()` | `RerankUnavailableError` |
| ONNX / tokenizer construction failure | `load_reranker()` | `RerankUnavailableError` |
| Loader probe failure | `load_reranker()` | `RerankUnavailableError` |
| Tokenization failure that is not an oversized query | `CrossEncoderReranker.score()` | `RerankUnavailableError` |
| Graph declares unsupported inputs | `CrossEncoderReranker.score()` | `RerankUnavailableError` |
| Inference failure (`session.run` raises) | `CrossEncoderReranker.score()` | `RerankUnavailableError` |
| Session returned no outputs (empty output list) | `_read_scores()` | `RerankUnavailableError` |
| Logits of unexpected shape | `_read_scores()` | `RerankUnavailableError` |
| Non-finite logit (`NaN`, `±inf`) | `_read_scores()` | `RerankUnavailableError` |
| Score-count mismatch | `rerank()` | `RerankUnavailableError` |
| Empty hydrated candidate pool | `search_reranked()` | `RerankUnavailableError` |
| Oversized query exceeding `RERANK_MAX_TOKENS` | `_encode_pairs()` | plain `IndexError`, exit 1 |
| Non-positive `top_k` / `limit` / `pool` / `rerank_in`, empty query, unknown mode, `--hyde --mode bm25`, non-positive `multi_query` | validation | plain `IndexError`, exit 1 |
| Scoring after `close()` | `CrossEncoderReranker.score()` | plain `IndexError` |
| Requested expansion cannot reach Ollama | `OllamaQueryExpander.probe()` | plain `IndexError`, exit 1 |
| Embedding-identity mismatch; embedder unreachable; missing chunks DB | retrieval layer | unchanged — outside the rerank boundary |

A missing artifact still starts transparent acquisition and emits the fetch
notice from §2. `load_reranker()` itself prints no failure warning; it raises
the typed cause. Only an explicitly lenient search boundary owns the fallback
warning, so strict callers receive one exception rather than an exception plus
duplicated stderr noise. Non-finite local logits are refused because `NaN`
would otherwise silently make ranking depend on input position.

## 10. CLI and library retrieval

`palace.search.search_reranked()` is the one-call library surface. `rerank_candidates()` and `search_expanded()` own scoring and hydration below it. A future MCP wrapper is a separate feature and requires its own implementation and qualification.

## 11. Evaluate before tuning

Committed fixture and question scaffolding do not establish representative workload relevance. Label unattributed benchmark aggregates clearly. The shipped default-on reranker is the named exception; representative evaluation remains required to confirm or reverse it and before changing token caps, pool depths, model or expansion posture. See `policies/acceptance-empirical.md`.

## 12. One home for knobs

The model name, model-directory resolver, input/output depths, default switch,
token cap, latency budget, expansion model, multi-query bare-flag count, and
Ollama endpoint all live in `palace/retrieval_config.py`. Policies document
them; callers import them; no caller re-declares them.
