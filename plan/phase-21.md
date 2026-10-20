---
id: '21'
title: Bounded indexing and recovery
depends_on: []
informs: []
review_lane: full
---

# Bounded indexing and recovery

## Identity correction

This is Phase 21, selected after reading the source-defined hashed advisory-budget identity and verifying that numeric IDs 16 through 20 were already used. The initial named entry/capture used `bounded-indexing`; a subsequent Phase 20 capture was mistakenly selected from an incomplete inventory and refused before provider launch because it collided with historical budget state. Both original sketches, timestamps, captures and parked runs remain preserved. Attributable operator relay `0c818a29ccbe4a359e05b14a608fe39d` authorizes the genuinely unused numeric mapping, fresh capture and one reviewed resubmission under exact Claude-subscription egress approval. No old hash or budget is rewritten and no successful review is claimed for either prior run.

## Commission

New scope authorized on 2026-10-20: the operator answered "Y" to implementing bounded fair chunk scheduling with atomic document visibility and durable recovery, plus stopping daemon checkpoint advancement at failures, with independent review and full validation. The attributable approval was supplied in Folded request `88410dccffeb40cd84ee94a6e3b22cc8`; answer `d03b776c93f84fa2820905068c673017` expressly authorized this new phase and ledger row. This is fresh work with fresh review history, separate from public release preparation.

## Goal

Make small documents searchable before earlier large documents finish, preserve completed embedding work through interruption, bound supported inputs/resources explicitly and keep each document's visible update atomic. Stop the sequential daemon at its first failed event so its saved cursor cannot cross unresolved work.

## Deliverables

- A bounded fair chunk scheduler for synchronous builds, with deterministic folding and completed-document publication across selected roots.
- Private durable receipts bound to source snapshots, exact embedding inputs and complete embedding identity, with bounded validated recovery.
- Declared admission/resource limits, visible oversized-source refusal and structured progress without changing chunk, embedding or provenance semantics.
- A separate prompt daemon first-error stop/park with safe shutdown and a checkpoint before the failed event.
- Fake/mocked-provider qualification within the existing proof inventory, updated operations documentation and attributable guarded delivery.

## Acceptance

- `./bin/test tests/index/test_core.py tests/index/test_writer_lock.py tests/test_search.py` proves bounded service, atomic visibility, exact input/vector/provenance correspondence, deterministic fold, durable receipt reuse and visible resource refusal without paid inference.
- The existing fake-provider writer/core proof family proves that a failed event prevents all later queued commits and cursor advancement, including idle-tail and full-queue shutdown cases. The separate live-Ollama indexing smoke is excluded.
- Crash-boundary and source/identity/corrupt-receipt cases preserve originals and reuse only validated completed work; source, staging and request bounds have explicit boundary witnesses.
- `./bin/test-governance validate` and the required frozen-corpus assay remain green with no unapproved proof growth.
- Independent plan/code advice, primary dispositions, candidate-bound full implementation gate and a second bare `./bin/check all` prove the final handoff candidate.
- No paid inference, production index mutation, credential/grant change, deployment or service restart occurs. No online identity-generation swap or arbitrary huge-source parser rewrite is included.

User Demo: With disposable sources and a fake provider, inspect progress showing a later small document committed while an earlier large one remains pending; interrupt and resume to see validated receipt reuse. Inspect oversized-source refusal and the daemon's failed-event park. Product/readability judgment remains with the operator; do not use production stores or paid providers for this demo.

## Brief refs

- [Project index generation](../briefs/project-index-generator.md)
- [Bounded remote embedding throughput](../briefs/remote-embedding-throughput.md)
- [Embedding provider and index identity](../briefs/embedding-provider-and-index-identity.md)
