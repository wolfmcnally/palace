---
id: '36'
title: Release 0.7.0 with the opt-in vector cache
depends_on:
  - phase-35.md
informs: []
review_lane: full
---

# Release 0.7.0 with the opt-in vector cache

## Commission

Part of the 2026-11-19 operator approval recorded in [Phase 35](phase-35.md), relayed through the authenticated factotum (18:52:25Z): release the opt-in exact in-memory vector cache so that a deployed consumer can integrate it. That instruction is the scoped tagging authority for `v0.7.0` and its GitHub release against the default tag restriction; it covers this one tag and release only. Commits and the tag are unsigned and use the repository's personal author identity per command, under the operator's 2026-11-17 direction. Review is the Phase 35 code advisory and its dispositions; the coordinator directed no further reviewer.

## Goal

Make Palace 0.7.0 exactly the tagged, qualified release of Phase 35's contract, so a downstream library can pin the tag. The default search is unchanged; the cache and the change feed stay opt-in.

## Deliverables

- Version 0.7.0 in every place 0.6.0 named its version, the lockfile in the same change, and a dated 0.7.0 changelog section from the Unreleased entries.
- `RELEASE.md` for 0.7.0 with a platform statement limited to what is actually run, and the package smoke extended to import the cache module from the installed wheel.
- The release procedure: packages built twice and compared, a clean hash-locked installation and the package smoke, the disclosure screen, `./bin/check all`, `./bin/check native` and `CHECK RELEASE VERIFIED` on the exact delivered commit, the annotated tag, tag CI, the GitHub release with the wheel, the source archive and `SHA256SUMS`, and anonymous re-download verification.

## Acceptance

- `./bin/check all` passes on the delivered commit, and `CHECK RELEASE VERIFIED` on that commit; native coverage is reported as actually run.
- Reproducible packages, the clean hash-locked install and the package smoke pass; the disclosure screen finds no consumer name.
- The annotated `v0.7.0` tag resolves to the qualified commit on the remote, and the downloaded assets re-hash equal to the qualified ones.
- Manual: the operator owns publication judgment beyond the scoped tag, consumer enablement and the dogfood outcome.

User Demo: N/A — release qualification and publication evidence; no new product surface beyond Phase 35.

## Brief refs

- [vector index cache](../briefs/vector-index-cache.md)
- [deployed consumer readiness](../briefs/deployed-consumer-readiness.md)
