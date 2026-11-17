---
id: '34'
title: Release 0.6.0 with typed provider throttling
depends_on:
  - phase-33.md
informs: []
review_lane: full
---

# Release 0.6.0 with typed provider throttling

## Commission

Part of the 2026-11-17 operator authorization recorded in [Phase 33](phase-33.md): tagged releases of everything owned that the throttling work touches, so a deployed consumer can integrate against tested tags. The operator's direct instruction ("release everything you need to touch that we own") is the scoped tagging authority for `v0.6.0` against the default tag restriction; it covers this one tag and its GitHub release only.

## Goal

Make Palace 0.6.0 exactly the tagged, qualified release of Phase 33's contract, with documentation that states its units and limits plainly, so a downstream library can pin the tag.

## Deliverables

- A capability brief for provider throttling and request pacing: who owns retry, pacing and outcome classification, the delivery vocabulary, the byte unit, the process-local scope of a pacer, what a deadline does and does not bound, and what is deferred.
- `docs/bedrock.md` and the index README, already updated in Phase 33 for the retry policy, the pacer, the per-provider per-operation deadline bounds, library-owned failure text and the byte request cap, re-checked against the released behavior, with the SDK minimum stated where the per-read bound depends on it.
- Version 0.6.0 in every place 0.5.1 named its version; a dated 0.6.0 changelog section; `RELEASE.md` for 0.6.0; the `boto3` floor raised to a version whose botocore supports per-request read timeouts, with the lockfile in the same change.
- The release procedure in `RELEASE.md`: packages built twice and compared, clean installations on macOS and Linux, the disclosure scan, `CHECK RELEASE VERIFIED` on the exact delivered candidate, the tag, tag CI, the GitHub release and public download verification, then the dogfood step after publication.

## Acceptance

- `./bin/check all` passes on the unchanged candidate, and `CHECK RELEASE VERIFIED` on the delivered commit; native coverage is reported as actually run.
- Reproducible packages, clean hash-locked installs and the package smoke pass; the disclosure scan finds no consumer name.
- The annotated `v0.6.0` tag resolves to the qualified commit on the remote, and the downloaded assets re-hash equal to the qualified ones.
- Manual: the operator owns publication judgment beyond the scoped tag, and the dogfood outcome.

User Demo: N/A — release qualification and publication evidence; no new product surface beyond Phase 33.

## Brief refs

- [bedrock embedding provider](../briefs/bedrock-embedding-provider.md)
- [deployed consumer readiness](../briefs/deployed-consumer-readiness.md)
