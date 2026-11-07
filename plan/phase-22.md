---
id: '22'
title: Bedrock embedding provider and composed settings
depends_on: []
informs: []
review_lane: full
---

# Bedrock embedding provider and composed settings

## Commission

Authorized on 2026-11-07 through an authenticated factotum-relayed operator direction to start Palace's own planning and implementation of a bounded capability requested upstream by a deployed multi-store consumer: a registered Bedrock embedding provider selected from a Palace-owned section of the top-level application's composed configuration. The operator's ruling is recorded in capability-class terms in the brief. The identity 22 was verified unused against the advisory-budget key and all run metadata. No new grant, credential, AWS resource, paid inference, live data transmission, deployment, publication, push or tag is authorized; delivery is restricted to a local commit.

## Goal

Let a consumer declare Cohere Embed v4 through AWS Bedrock as a store's embedding identity from the `palace` section of a composed configuration it hands Palace, with the Region's data-retention mode checked fail-closed before any request, documents and queries embedded with their distinct input types, the store bound to the model's dimension, and every request bounded, audited and never silently substituted.

## Deliverables

- A per-store vector width: the chunks vector table declared from the store's recorded identity dimension, with an explicit refusal when an existing table was created at another width.
- A registered `bedrock` embedding provider: direct regional model id, AWS SDK credential chain only, same-Region `GetAccountDataRetention` check requiring `none` before the first embedding request, Cohere Embed v4 request body with purpose-specific `input_type`, batches of at most 96 under an aggregate request budget, bounded concurrency and retries with SDK retries disabled, audited attempts, and the existing published-corpus boundary.
- A Palace-owned composed-settings module: a Conric leaf validator for the top-level `palace` section sharing one mapping validator with the TOML selector, a loader taking composed files and root, and a once-only store initialization that refuses a differing existing selector.
- Purpose-aware embedder resolution (`document` or `query`) at every build, update, daemon and search call site, with the protocol unchanged for injected embedders.
- Conric as a pinned runtime dependency with the lockfile, release checklist and CI install adjusted for a Git-pinned package; documentation of the `palace` section, the provider, the retention check's limited meaning and the Region constraint; pinned service-model excerpts; updated changelog and consumer-readiness brief.
- Offline hermetic proofs admitted with compensating retirements.

## Acceptance

- `./bin/test tests/test_bedrock_provider.py tests/test_private_providers.py tests/test_search.py tests/index/test_core.py` proves: the `palace` section parsed from a composed file set and refused on unknown fields; selector validation refusing unknown fields, a missing Region, an inference-profile model id and a dimension outside 256/512/1024/1536; the identity stamp for a `bedrock` selector; refusal at `probe()` without credentials; refusal when the retention mode read is anything but `none` against a stubbed client, with no embedding request made; the exact request bodies for a document batch and for a query; batching at 96; no request from configuration alone; a 1536-dimension store built and searched end to end through the stubbed client; and unchanged behavior for 4096 stores.
- `./bin/test-governance validate` stays green with every new proof funded by a recorded retirement.
- Independent plan and code advice are ingested with primary dispositions; the candidate-bound full implementation gate and a second bare `./bin/check all` prove the handoff candidate.
- No AWS call, paid inference, credential, grant, deployment, push or tag occurs.

User Demo: From the repository, write a two-file composed configuration whose `palace.embedding` selects `bedrock` with `cohere.embed-v4:0`, Region `us-east-1` and dimension 1536, run `./bin/python -m palace.cli providers set --operation embedding --store .demo-bedrock --settings defaults.yaml --settings site.yaml` and inspect `.demo-bedrock/meta/embedder.toml` and `./bin/python -m palace.cli index embedder show --store .demo-bedrock`. Change the dimension to 1000 and repeat: the refusal should name the allowed dimensions. Remove `region` and repeat. Add an unknown key under `palace` and repeat. Judge whether the refusals and the documented section shape are clear enough to hand to an operator; no network request is made at any step and no credential is read. Delete `.demo-bedrock` afterwards.

## Brief refs

- [Bedrock embedding provider and composed settings](../briefs/bedrock-embedding-provider.md)
- [Embedding provider and index identity](../briefs/embedding-provider-and-index-identity.md)
- [Bounded remote embedding throughput](../briefs/remote-embedding-throughput.md)
- [External-consumer readiness](../briefs/deployed-consumer-readiness.md)
