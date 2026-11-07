---
id: '23'
title: Release 0.3.0 preparation
depends_on:
  - phase-22.md
informs: []
review_lane: light
---

# Release 0.3.0 preparation

## Commission

Authorized on 2026-11-07 through an authenticated factotum-relayed operator direction: continue permitted local release preparation without publishing, under the repository's gates, after the workstation lane of the Bedrock provider's live qualification passed. Publication (push, tag, GitHub Release) follows the still-required instance-role qualification and is not part of this phase. Delivery is restricted to a local commit. The identity 23 was verified unused against the advisory-budget key.

The light review lane is declared because every deliverable is a version bump, a changelog section, documentation wording, or a packaging check that the existing gates already prove; no public API, schema, persisted state, concurrency, security surface or product behavior changes. The evidence lane stays full because `.github/package-smoke.py` is a deploy seam.

## Goal

Make the tree exactly what the 0.3.0 release would tag: version 0.3.0 everywhere the 0.2.0 release named its version, a dated 0.3.0 changelog section, release and installation text that matches the Git-pinned dependency procedure, a clean sanitization result for the current tree and the built artifacts, and locally verified packages and clean-install smoke, so that the later publication step is a push, a tag and the established workflow with no further source change.

## Deliverables

- Version 0.3.0 in `pyproject.toml`, `uv.lock`, `.github/package-smoke.py`, `README.md`, `RELEASE.md` and the changelog, with the `Unreleased` section renamed to `0.3.0` and dated.
- A sanitization record: a bounded scan of the current tree and the built artifacts for consumer names, private paths, account identifiers and credential shapes, with its exact command and result recorded in the END block; secret-scanning tools remain unavailable on this machine and that gap is stated, not hidden.
- Locally built 0.3.0 artifacts (sdist and wheel) rebuilt twice byte-identically from the same tree, a hash-locked clean installation that excludes and separately installs the Git-pinned dependency, and the package smoke passing in that environment on macOS.
- Release documentation consistent with the artifacts: the checklist's version references and the installation steps for the Git-pinned dependency.

## Acceptance

- `./bin/check all` passes on the unchanged candidate.
- `uv build --managed-python --out-dir <outside the tree>` produces `palace-0.3.0.tar.gz` and `palace-0.3.0-py3-none-any.whl`; a second build into another directory yields byte-identical archives (compared by SHA-256).
- `uv export --frozen --no-dev --no-emit-project --no-emit-package conric --format requirements-txt` succeeds, `uv pip install --require-hashes` from it succeeds in a fresh managed venv outside the tree, the pinned Conric commit installs with `--no-deps`, the 0.3.0 wheel installs with `--no-deps`, and `.github/package-smoke.py` passes from outside the checkout.
- A bounded scan of the tree and both artifacts finds no consumer name, private path, account identifier or credential shape; the exact command and result are recorded.
- No push, tag, release, registry upload, deployment or AWS call occurs.

User Demo: N/A — this phase changes version strings, documentation and packaging evidence only; the product surfaces were demonstrated in Phase 22 and publication is a later, separately authorized step.

## Brief refs

- [Bedrock embedding provider and composed settings](../briefs/bedrock-embedding-provider.md)
- [External-consumer readiness](../briefs/deployed-consumer-readiness.md)
