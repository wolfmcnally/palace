---
title: Build both trees at one work path when comparing Palace outputs across source
  trees
scope: local
proposed_surface: brief
source: kickoff
slug: daft-kangaroo
status: candidate
filed: '2026-11-21'
occurrences:
- date: '2026-11-21'
  ref: 'distinct-source-results baseline parity: two runs of the released tree in
    fresh temporary directories differed in chunk ids and order'
---

As of 2026-11-21, a before/after parity script ran each Palace source tree against stores built in a fresh random temporary directory. Chunk ids hash the absolute watch-root path, so the two runs produced different ids and therefore different tie order and fused ranking. Even two runs of the same released tree disagreed, which first looked like a regression in the candidate.

When comparing serialized search output across trees or runs, build every store at an identical absolute work path (recreated per scenario) and first prove the baseline reproduces against itself. This is an unratified candidate.