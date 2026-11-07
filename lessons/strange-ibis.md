---
slug: strange-ibis
title: A version-bump inventory must search regex-escaped forms and the previous release commit's own file list
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-07
source: kickoff
occurrences:
  - date: 2026-11-07
    ref: "Phase 23 planning: the orchestrator's plain-literal inventory missed tests/test_smoke.py (`0\\.2\\.0`) and .github/workflows/ci.yml"
---

The phase sketch listed the files that named the version, written from a plain-literal search for `0.2.0`. The planner found two more sites: a smoke test that stores the version as a regex-escaped pattern, and the CI workflow's wheel filename. A plain-literal grep cannot find an escaped version, and the sketch's file list was a proxy for the goal's "everywhere the previous release named its version".

The authoritative inventory for a version bump is the previous release's own preparation commit (`git show --stat` of it) plus a search for both the plain and the regex-escaped forms of the old version, with the `tests/` tree included even when "nothing in the test tree changes" seems true. One observation; a candidate lesson, not a skill change.
