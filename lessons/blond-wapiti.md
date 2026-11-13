---
slug: blond-wapiti
title: Compare concurrent event clients from the raw event log, not from flags sampled at the first arrival
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "FSEvents flush diagnostic: per-cycle booleans reported watchdog missing events that its raw callback log showed arriving within 0.6 ms of the independent stream"
---

As of 2026-11-12, a scratch diagnostic compared an independent CoreServices stream with watchdog's stream. It read watchdog's "seen" flag immediately after the first client reported the target, so in five of fifteen cycles watchdog appeared to miss an event that its raw callback log recorded a fraction of a millisecond later, with the same event id. The summary row was wrong; the retained raw log was right, and the report was corrected.

When comparing two asynchronous clients of the same event source, record every callback with its timestamp and event id, and derive comparisons from that log after a settle window. Never derive them from a flag sampled at the moment the other client fired. This is an unratified candidate.
