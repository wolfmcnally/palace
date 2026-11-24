---
id: us-bedrock-profile
title: US geographic Bedrock profile for Embed v4
depends_on:
- distinct-source-results
informs: []
review_lane: full
order: '19'
parent: null
---

# US geographic Bedrock profile for Embed v4

## Commission

Authorized on 2026-11-24 by the operator, relayed through the authenticated factotum (18:32:13Z, sentinel ee3819c3f690819197611ec0a10f6b68), as the next Palace patch release after 0.8.1. The operator's binding requirements, in generic terms: the Bedrock embedding selector accepts exactly two model ids, the existing direct model `cohere.embed-v4:0` and the US geographic inference profile `us.cohere.embed-v4:0`; a new Bedrock configuration that omits the model uses the US profile; an explicit direct pin keeps working unchanged; no arbitrary, global or other geographic profile is accepted; the supported source Regions are documented and an unsupported one fails clearly; invoke routing and the store's embedding identity stay consistent, with no silent switching between routes and no reuse of existing direct vectors without proven compatibility, preferring a distinct conservative identity for the new route and documenting re-embedding while existing explicit direct stores are preserved; embedding only, with no reranker, permission, custody or throttling change and no AWS IAM change. No consumer is named here.

## Goal

A store's Bedrock selector can name the US geographic profile, explicitly or by omitting the model, and Palace invokes exactly that profile from one documented US source Region. The store's identity records the profile so its vectors never mix with a direct-model store's. A direct store and its configuration behave exactly as in 0.8.1.

## Decisions pinned by this phase

- The model allowlist is exactly `cohere.embed-v4:0` (direct) and `us.cohere.embed-v4:0` (US geographic profile). Every other id, including `eu.`, `apac.` and `global.` profiles, profile ARNs and other models, is refused before any client is constructed.
- The Bedrock selector's `model` becomes optional on every configuration route (composed `palace` section and the TOML selector file) and defaults to `us.cohere.embed-v4:0`. A saved selector always records the model explicitly, so a store created from an omitted model keeps its route if the default ever changes.
- The US profile is admitted only with a documented US source Region: `us-east-1`, `us-east-2`, `us-west-1` or `us-west-2` (AWS Bedrock model card `model-card-cohere-embed-v4.html`, retrieved 2026-11-24). Any other Region with the US profile, including an omitted model with a non-US Region, refuses with text naming the supported Regions and the direct-model remedy. The direct model keeps 0.8.1's Region handling.
- `InvokeModel` is sent to the source Region's standard runtime endpoint with `modelId` equal to the selected id. Endpoint binding, credentials, bounds, retries, pacing and the audit are unchanged.
- Identity stays `provider = bedrock:<source region>` and `model = <selected id>`, so a US-profile store and a direct store, or two US-profile stores with different source Regions, never share an identity. Moving a store between routes is a re-embedding: the existing selector refusal and rebuild remedy apply, and nothing reuses existing vectors.

None of these departs from the commission; the source-Region list is a dated service fact.

## Deliverables

- The two-id allowlist, the optional model with the US default and the US source-Region check, in the shared selector validator and index configuration.
- Hermetic proofs on both configuration routes: an omitted model selecting the US profile; an explicit direct pin unchanged; an explicit US profile; unsupported routing refused with clear text (other geographic, global and ARN profiles; the US profile or an omitted model with a non-US Region); the distinct identities; a composed configuration that omits the model refused against an existing direct store; and the exact `InvokeModel` model id and runtime endpoint for both routes.
- Documentation in `docs/bedrock.md`, the Bedrock brief and the changelog: the two routes, the source and destination Regions, the identity consequence and the re-embedding procedure.

## Acceptance

- Every existing proof passes; an explicit direct selector's validated configuration, identity and requests equal 0.8.1's.
- The proofs above pass, each observed red under a planted defect.
- No AWS or model request is made by any proof; no reranker, permission or custody surface changes.
- Focused tests and both full gates pass against their fixed candidates, with the independent plan review and code critique and their dispositions retained.
- Manual: a live call through the US profile from a real account is outside this phase and not run; release acceptance is the operator's.

User Demo: N/A — configuration validation and request shape proved by deterministic offline tests; no live AWS call.

## Exclusions

- Any other inference profile, cross-geography or global routing, reranking through Bedrock, IAM, permission or custody changes, and automated throttling.
- Any live AWS or model call.
- The release itself (a separate release step) and any consumer integration.

## Brief refs

- [bedrock embedding provider](../../briefs/bedrock-embedding-provider.md)
- [embedding provider and index identity](../../briefs/embedding-provider-and-index-identity.md)
