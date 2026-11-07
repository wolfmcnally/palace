---
title: "Bedrock embedding provider and composed Palace settings"
date: 2026-11-07
status: implemented
scope: "Public technical design for a registered AWS Bedrock embedding provider (Cohere Embed v4) selected through a Palace-owned composed-configuration section; dated service observations and their limits; no consumer inventory or deployment evidence."
---

# Bedrock embedding provider and composed Palace settings

As of 2026-11-07. A deployed multi-store consumer asked for a way to declare Cohere Embed v4 through AWS Bedrock as a store's embedding identity. The operator ruled that every dependency stack they own is configured through one composed configuration at the top-level application, so the capability arrives as a Palace provider selected from a Palace-owned section of that composition, not as an embedder the consumer builds and injects. This brief records what Palace concluded from the service documentation and what the design therefore is. Retention, cost and availability facts below are dated observations, not guarantees.

## The composed `palace` section

Palace owns and validates a top-level `palace` mapping inside a [Conric](https://github.com/wolfmcnally/conric) composition (public MIT, pinned at commit `ed9286297e92b8c049f70eb15a3b5556ed107ff0`, retrieved 2026-11-07). A consumer hands Palace the composed files and their root exactly as it loaded them; Palace reads only its own section, refuses unknown fields, and derives the store's embedding selector from it. The same mapping shape is accepted from the TOML selector file, so there is one validator and one documented schema. A `SecretRef` (`!env NAME`) is accepted only where a selector already takes a credential *reference*; Palace never resolves it.

Initialization writes the store's selector once. An existing selector that already equals the composed one is left alone; a different one is refused with the rebuild remedy rather than overwritten, because the selector is the store's recorded identity source and a silent change would desynchronize it from the vectors already stamped. Loading or validating configuration makes no inference request and reads no credential.

## The `bedrock` provider

- **Model.** A direct Bedrock model id on the regional runtime endpoint (`cohere.embed-v4:0`). Inference-profile ids (`us.`, `eu.`, `global.` prefixes) are refused: a profile may process the request in a destination Region other than the one the selector names, which would detach the retention check from the request. As of 2026-11-07 the Embed v4 model card lists direct in-Region access only in `us-east-1`, `eu-west-1` and `ap-northeast-1` (AWS Bedrock user guide, `model-card-cohere-embed-v4.html`, retrieved 2026-11-07; no page date). Availability is a service fact and is not hardcoded; an unavailable Region surfaces as the service's own refusal before any write.
- **Authentication.** The AWS SDK credential chain only (instance role, named profile, environment). The selector may name a profile; it never carries a secret. A missing or expired credential refuses at `probe()`.
- **Retention check, fail closed.** Before the first embedding request a provider instance reads `GetAccountDataRetention` on the Bedrock control plane in the selector's Region and refuses unless the mode is exactly `none`. The user guide states the setting is per-Region and that a Region never configured remains `inherit`; the API reference calls it account-wide. Palace follows the more specific per-Region statement and checks the Region it will call. **What the check means:** it reads one Region's configured mode at one moment. It does not observe what the service retains, cannot see what a cross-region profile does in its destination Regions, does not read any per-model override, and is not a substitute for the account administrator's configuration or a compliance attestation. Palace documents this limit wherever the provider is described. (AWS Bedrock API reference `API_GetAccountDataRetention.html` and user guide `data-retention.html`, retrieved 2026-11-07; enum `default | none | aws_review | provider_data_share | inherit` matches the pinned service model.)
- **Request shape.** `InvokeModel` with a JSON body: `texts` (at most 96 per request), `input_type` = `search_document` for indexing and `search_query` for a query, `embedding_types` = `["float"]`, `truncate` = `"NONE"`, and an explicit `output_dimension` equal to the selector's `dim` (allowed values 256, 512, 1024 and 1536; the model's default is 1536). With a single embedding type the documented response carries `embeddings` as an array of float arrays and `response_type` `embeddings_floats`; AWS's own sample code reads the by-type object shape instead, so Palace accepts both documented shapes and refuses anything else. (User guide `model-parameters-embed-v4.html`, retrieved 2026-11-07.)
- **Asymmetric input types.** The embedder protocol is unchanged. Palace resolves a store's embedder for a declared purpose, `document` or `query`; the Bedrock provider carries that purpose into `input_type`, and symmetric providers ignore it. Callers that inject their own embedder keep owning that choice.
- **Dimension and identity.** A store binds one dimension and its vector table is declared at that width. The identity stamp is `provider = bedrock:<region>`, `model` = the model id, `dim` = the selector dimension, plus the shared text convention. The Region is part of the provider string on the conservative side: Palace has not measured whether the same model returns identical vectors across Regions, and widening an identity later is cheap while narrowing one invalidates stores. Equal dimensions never make vectors interchangeable.
- **Bounds.** Batches of at most 96 inputs and a conservative aggregate request budget counted in the same raw-or-NFC UTF-8 units Palace already uses for remote requests, kept below the model's documented 128K-token context so that no tokenizer whose tokens are at least one byte can exceed it; bounded concurrency with an operator-lowerable ceiling; bounded retries with backoff on throttling, unavailability, internal, model-timeout and connection failures only. SDK retries are disabled so every attempt is Palace's: audited, counted and visible.
- **Audit.** Every control-plane and runtime attempt appends a `cloud-egress` record without corpus text, query text or credentials. The retention read is audited like any other cloud call.
- **Boundary.** Bedrock is third-party inference, so the existing published-corpus opt-in and the unconditional personal-store and personal-vault refusals apply unchanged. A Bedrock failure is a refusal, never a substitution.
- **Probe.** `probe()` is the authenticated retention read, not an embedding. Model availability and the returned dimension are therefore first observed on the first real batch, which is validated before any row is written.

## Workstation qualification observations

As of 2026-11-07 (retrieved live, 15:40:47Z to 15:41:07Z). Route: a workstation profile resolving to an operator-controlled development account, explicit Region us-east-1. Read-only prerequisites immediately before the run: caller identity confirmed; `GetFoundationModelAvailability` for `cohere.embed-v4:0` reported agreement AVAILABLE, authorization AUTHORIZED, entitlement AVAILABLE, region AVAILABLE; `GetAccountDataRetention` in that Region reported mode `none` (last updated 2026-09-02). The installed AWS CLI 2.34.4 does not know the retention operation; the read used botocore 1.43.104, the call Palace itself makes.

Fixture: ten synthetic Markdown notes totalling 2,526 bytes with one planted marker phrase and no operator data, treated as a published corpus for this bounded test. Settings composed from two YAML files through `palace providers set --settings`; stamped identity `bedrock:us-east-1` / `cohere.embed-v4:0` / 1536 / the shared text convention. `palace index build --full`: 10 documents, 11 chunks, 10 embedding requests, 0 failures, concurrency 4, 1.18 s. `palace search --mode vector --no-rerank`: the planted note ranked first of ten.

Observed provider behavior: one retention read per provider instance before any embedding call (build probe and search); documents sent as `search_document` (10 requests of one document each), the query as `search_query` (1 request); every response carried 1536 finite unit-norm floats, stored in a `FLOAT[1536]` table beside the recorded identity rows. All 13 audit records are HTTP 200 on the first attempt (no retries) and carry provider, model, dimension and input type without text, query or credential. The provider returns no usage figure, so cost is recorded unknown; a byte upper bound puts the run under 3,000 billed tokens, far below the USD 1.00 cap approved for it, at the rate the account's owner had previously observed.

Not established by this run: instance-role authentication, other Regions, per-model retention, cross-Region vector equality, throughput, or any property of a real corpus. The instance-role lane remains a separate operator-approved action.

## Per-store vector width

Before this work the vector table was declared at the local default's 4096 width and vector reads refused any other recorded dimension, so no provider could store a narrower vector without padding. Padding would preserve cosine distances but would put a second, false width into the identity. The table is now declared from the store's recorded identity dimension at initialization; a store whose existing table was created at another width is refused with the remedy of rebuilding from an empty chunks database. Existing 4096 stores create byte-identical DDL.

## Release and dependency consequences

Conric is pinned by Git URL, so a hash-locked installation of Palace's dependencies cannot include it. The release and CI procedures export the locked requirements without the Conric package and install it separately from the same pinned commit with no dependency resolution; its only dependency, PyYAML, is already hash-locked.

## What remains outside this brief

The workstation lane of live qualification ran once under an operator-approved USD 1.00 cap (observations above); the instance-role lane remains a separate operator-approved paid action. Neither is performed by the repository gate. Marketplace agreements, the Region's retention configuration and the IAM grants `bedrock:InvokeModel` and `bedrock:GetAccountDataRetention` are administrator actions. Reranking through Bedrock, inference profiles, image inputs and other embedding types are not included.

## Sources

- Pinned service-model excerpts: `docs/reference/botocore-bedrock-service-model-2023-04-20.json` (GetAccountDataRetention) and `docs/reference/botocore-bedrock-runtime-service-model-2023-09-30.json` (InvokeModel), from botocore 1.43.104.
- AWS Bedrock user guide pages `data-retention.html`, `model-parameters-embed-v4.html` and `model-card-cohere-embed-v4.html`, and API reference `API_GetAccountDataRetention.html`; retrieved 2026-11-07, no page dates published; not redistributed here.
- Conric manual and API reference at the pinned commit, retrieved 2026-11-07.
