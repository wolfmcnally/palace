# Local-first

Palace may process private operator-owned corpora. Local inference and local storage are the default. Sending corpus data across a boundary requires the explicit, store-scoped authorization below; no consumer or deployment history is asserted by this policy.

## Default: on-device

Palace's default for every cost-of-doing-business component is **on-device on Apple Silicon**:

| Component | Default | Acceptable alternatives |
|---|---|---|
| **Embeddings** | `qwen3-embedding:8b` via Ollama today; `nomic-embed-text-v1.5`, `bge-m3`, and MLX-served equivalents remain permitted | OpenAI `text-embedding-3-small` as opt-in fallback or ground-truth check; OpenRouter index-time embedding only as explicit per-store opt-in for an already-published corpus |
| **Cross-encoder reranking** | `ms-marco-MiniLM-L-6-v2` or equivalent, local | Cohere rerank-v4 or Jina rerank-2 as opt-in fallback |
| **Lightweight extraction** (fact candidates, summaries from raw events) | Local model via Ollama or MLX (Llama 3.x, Qwen, Mistral class) | Cloud model when local quality is documented-insufficient on a specific task |
| **Heavy synthesis** (research briefs, consolidator audits, contradiction resolution) | Cloud model (Claude, GPT-5.5) | Local model is acceptable but lower quality is expected |
| **Storage** | Local filesystem: `~/palace-data.noindex/` (machine state) and `~/Obsidian/Palace/` (human-readable Markdown) | None for palace's own machine state and the personal vault, which never live in a third-party cloud. A store opted in as published may publish its derived vector index to an operator-controlled cloud account (see § Published vector indexes) |
| **Indexes** | sqlite-vec, BM25 (sqlite-fts5 or tantivy), eventually LadybugDB | A published store's vector index in an operator-controlled vector store (Amazon S3 Vectors), only through the explicit per-store opt-in in § Published vector indexes; every other derived index is local |

## When cloud is acceptable

Cloud calls are acceptable when **any** of:

1. The task is **synthesis** over already-retrieved local context (e.g., the consolidator asking Claude to score a candidate set). The corpus stays local; only the relevant slice leaves the machine, and only for the duration of the call.
2. The task is a **ground-truth quality check** the local stack is being evaluated against (e.g., re-embedding a sample with OpenAI to verify the local model isn't drifting).
3. the operator has **explicitly opted in** for that workload via configuration.
4. Local capability genuinely does not exist for the task and is documented as a known gap.

### Public vs. published

These two words are not synonyms, and the remote-embedding boundary turns on
the second one:

- **Public** — ungated, open to the general public. Anyone with the URL reads
  it, no account and no credential.
- **Published** — served from internet-reachable infrastructure, whether or not
  access is gated. A subscriber-only archive, a customer-portal manual, and a
  staging site behind HTTP basic auth are all published without being public.

The criterion for bulk remote embedding is **published**, not public. Once a
corpus is served from cloud infrastructure and queried through a remote
embedder, its text already transits third-party infrastructure as a matter of
routine operation; index-time embedding through the pinned zero-data-retention
upstream adds no new exposure class to it. A corpus that is *not* published —
that lives only on the operator's disk — has no such existing transit, and sending it
would create the exposure rather than reuse one already accepted.

Bulk remote embedding is therefore permitted only for a store whose corpus is
already published, and only through an explicit per-store opt-in that records
what is published and where. The default personal store and every watch root
that overlaps the personal vault in either direction are refused
unconditionally; that boundary is untouched by the published criterion.

### Published vector indexes

The storage rule is restated as follows (operator direction, 2026-09-28, which found the earlier "palace storage never lives in a third-party cloud" rule outdated for deployed stores). Palace's own machine state and the personal vault never live in a third-party cloud. A store opted in as published may publish its derived vector index to a vector store in an operator-controlled cloud account, through the same kind of explicit per-store opt-in the remote-embedding boundary uses: an assertion, recorded with the store, of what is published and where, stamped with the time it was made. The default personal store and every watch root that overlaps the personal vault in either direction remain refused unconditionally; the opt-in cannot lift that boundary.

What leaves the machine is the store's vector for each chunk, keyed by the chunk's content-addressed id, and — only when the operator asks for it at publish time — that chunk's text, watch-root-relative path and heading as non-filterable metadata. The published index stays a derived view: the local store remains its source, a receipt recorded with the store names the index it was published to and the embedding identity its vectors were built with, and republishing rebuilds it. Querying the published index sends only the query embedding, built with the store's recorded embedding identity; a mismatch between that identity and the receipt refuses the query rather than substituting a provider.

Every call to the vector store, whether it publishes, deletes, lists or queries, is recorded in `events/cloud-egress/` with the provider, the operation, the target index, the request size and a digest of the request, and never with corpus text, query text or credentials. As with other cloud calls, an audit record that cannot be written refuses the operation.

### What cloud is never acceptable for

Cloud calls are **not** acceptable for:

- Bulk embedding of the personal vault to a cloud provider. An
  already-published corpus is outside this prohibition only through the
  explicit per-store boundary above.
- Mirroring `~/palace-data.noindex/` or `~/Obsidian/Palace/` to any third-party cloud store other than backup targets the user explicitly controls (iCloud, Time Machine, or their NAS).
- Any "free tier" service whose terms grant the provider rights to train on or otherwise mine the submitted text.

## Provider rules

When a cloud provider is invoked:

- The call sends **the minimum necessary** for the opted-in task: no conversation
  history or unrelated documents. For a published-corpus indexing task, the
  necessary input is the corpus itself, sent as measured, operator-visible,
  bounded concurrent embedding batches. Concurrency changes only how many of
  the same requests are in flight — never which text is sent, the retention
  setting applied, the upstream pinned, or the per-request audit record written.
- The provider's **retention setting is set to zero or the lowest available**. If the provider does not support zero retention, document the call in a brief and surface it to the operator before making it routine.
- API keys live in `~/.openclaw/` or a comparable secure location, **never** in `~/palace-data.noindex/` or `~/Obsidian/Palace/` (both are backed up; secrets aren't).
- The implementation records each cloud call in `events/cloud-egress/` with
  provider, model, token count, and the digest of the input, so the operator can audit
  cloud egress after the fact. This binds every caller, including operator
  diagnostics and measurement tools.

## Private deployment inference

An explicitly configured operator-controlled HTTPS endpoint may embed and rerank a deployed store without a published-corpus assertion. This is a separate authorization class from third-party inference: the selector must attest the controlled boundary, its rationale and a timestamp, select the native `palace-json-v1` adapter, and reference credentials without storing their values. A URL, label, credential or sibling checkout never constitutes that authorization. The operator owns verification of deployment control, retention and access. The default personal store and watch roots overlapping the personal vault remain unconditionally refused. Third-party OpenRouter retains its existing published-corpus boundary.

Both operations verify TLS, disable redirects and ambient proxies, bound attempts and responses, validate model/provider identity and complete finite output, and append attempted-request and response audit records without corpus text or credentials. Audit failure refuses; no alternate provider is constructed. Configuration alone makes no inference request. Exact schemas and operational limits are in [private providers](../docs/private-providers.md). Private endpoint support is synchronous on macOS and Linux; it does not authorize a remote index daemon or change retrieval defaults and evaluation prerequisites.

## Fallback behaviour

Configured inference providers are never silently substituted. Local defaults continue to function offline when their required local services and artifacts are available. An unavailable explicitly selected remote provider refuses the operation, subject only to the visible reranking degradation described below.

Identity-bound single-store and multi-store retrieval query each store with its selected and recorded model, provider, dimension and convention. A missing, incompatible or unreachable required provider refuses the whole request. Local substitution would query a different vector space and cannot preserve the requested result.

When local reranking fails operationally, the single-store CLI returns fused RRF with an explicit `failed` status and one stderr line naming the cause; `search_reranked()` defaults to strict failure and raises. Private reranking and multi-store retrieval are strict in both the library and CLI. A caller must explicitly select `strict=False` or `--allow-rerank-failure` to receive degraded fused results with `failed` status for operational unavailability. Private authentication, identity, schema and audit errors always refuse. `--no-rerank` reports `disabled`. No caller can mistake a degraded answer for an applied rerank.

## Opt-in configuration

Specific cloud usages the operator can enable per-component will live in a `palace.config` file when one exists. Until then, the rule is: any new cloud dependency requires a brief or a note to the operator, never a silent default.

## Why this is a policy, not a preference

Local-first is the privacy and sovereignty posture palace was designed around. Drifting toward "just use the OpenAI API, it's fast" turns palace into yet another personal-memory SaaS in long pants. The local stack on Apple Silicon in 2026 is genuinely competitive (see `briefs/sota-memory-and-recall.md` §B.8); cloud is a tool palace reaches for deliberately, not a default it leans on.
