# MEMORY

All people, projects, deployments, and operational events below are fictional test data.

Durable bitemporal facts promoted by the palace consolidator, per `policies/fact-schema.md`. Hand-edits are limited to `valid` and `superseded_by`; everything else is immutable once written.

## Juniper Observatory uses master as its default branch

---
id: f1eed1a9cbc2f81b9722a8b31e8ef6afc7fbeaf60db009a8413e4b4c2d105743
event_time: 2026-05-17
ingest_time: 2026-05-17T09:00:00-06:00
confidence: 0.95
valid: true
provenance:
  - event:1a2b3c4d5e6f7a8b
tags: [decisions, infrastructure]
refs:
  - "[[simulation-guide]]"
---

The fictional Juniper Observatory uses `master` as its default branch; its simulated repository creator must choose master rather than main.

## Calibration tool has an editable development installation

---
id: b8178173c5c5db5faed5148581fe607d826aaacfb25839821e72a01b76d06fab
event_time: 2026-05-18
ingest_time: 2026-05-18T10:15:00-06:00
confidence: 0.9
valid: true
provenance:
  - event:2b3c4d5e6f7a8b9c
  - event:3c4d5e6f7a8b9c0d
tags: [infrastructure, decisions]
refs:
  - "[[tooling]]"
  - "[[simulation-guide]]"
---

The fictional observatory installs its calibration command in an editable development environment so source edits take effect without rebuilding.

## Sample capture is immediate and quality scoring is deferred

---
id: 3edfb0bb3e43eec20c15d515dcc5ab34caef8f201073385792a7f826184f1b16
event_time: 2026-05-18
ingest_time: 2026-05-18T11:30:00-06:00
confidence: 0.88
valid: true
provenance:
  - event:4d5e6f7a8b9c0d1e
tags: [architecture, decisions/pipeline]
refs:
  - "[[sota-memory-and-recall]]"
---

The fictional observatory records telescope samples immediately and performs quality scoring in a later batch process.

## Juniper Observatory adopted sqlite-vec after evaluating alternatives

---
id: 4978413d4e2d4c4fbfef4e6f817d1a14ce396595d6059ba72b39b55356f23e13
event_time:
  start: 2026-04-01
  end: null
ingest_time: 2026-05-19T14:33:00-06:00
confidence: 0.82
valid: true
provenance:
  - event:b7e3a1c4d2e5f607
  - event:5c01ff9d8e7a6b5c
tags: [juniper-observatory, infrastructure, decisions]
refs:
  - "[[training-project/architecture]]"
  - "[[sqlite-vec]]"
---

The fictional Juniper Observatory adopted sqlite-vec on 2026-04-01 after evaluating Qdrant and pgvector for its small synthetic star catalog; it rejected a separate Redis service.

## Observatory catalog uses hybrid retrieval

---
id: 61f3f1d156ae2a476a6af2b27e375d3e33852a654971ddd678ee2e3d8960a7af
event_time:
  start: 2026-05-24
  end: null
ingest_time: 2026-05-24T16:00:00-06:00
confidence: 0.9
valid: true
provenance:
  - event:6d7e8f9a0b1c2d3e
tags: [infrastructure, decisions]
refs:
  - "[[search]]"
---

The fictional observatory combines keyword rows and dense vectors using Reciprocal Rank Fusion over a disposable synthetic catalog.

## Observatory stores calibration facts as prose with frontmatter

---
id: dfb41d8e3525c5c7b03718182e935eb9fee644a38f31575b76304fddf931801d
event_time:
  start: 2026-05-20
  end: null
ingest_time: 2026-05-25T08:45:00-06:00
confidence: 0.85
valid: true
provenance:
  - event:7e8f9a0b1c2d3e4f
tags: [decisions, architecture]
refs:
  - "[[fact-schema]]"
  - "[[observatory/calibration]]"
---

The fictional observatory stores calibration facts as natural-language Markdown sections with YAML frontmatter rather than graph triples.

## Disposable catalog was rebuilt with relative sample paths

---
id: 8deb05354df33080d42b0a4c515d5f4d29d343c80e06164e2bf7b8d7b19fb8a4
event_time:
  start: 2026-05-24
  end: 2026-05-24
ingest_time: 2026-05-25T09:30:00-06:00
confidence: 1.0
valid: true
provenance:
  - event:8f9a0b1c2d3e4f50
  - event:9a0b1c2d3e4f5061
tags: [infrastructure, operations]
refs:
  - "[[simulation-notes]]"
---

The fictional observatory rebuilt a disposable catalog on 2026-05-24 to replace absolute sample paths with relative paths; it contained 24 samples across 6 invented files.

## Demonstration scheduler runs on a separate simulator

---
id: 0ba52f06e0633a75a6bb364a5cbd245ead65acdb902dbf14cd5e1b980093cf99
event_time: 2026-05-18
ingest_time: 2026-05-26T10:00:00-06:00
confidence: 0.7
valid: false
provenance:
  - event:0b1c2d3e4f506172
tags: [infrastructure]
refs:
  - "[[demonstration-scheduler]]"
---

The fictional observatory runs its demonstration scheduler on a separate simulator pending a move to the catalog simulator.

## Observatory planned a third-party lookup service

---
id: 7eeeb55cff3e63379b0e82617d9fe9ca2e4f60679d91577ea07c0f9d31ffd7d4
event_time:
  start: 2026-05-10
  end: null
ingest_time: 2026-05-27T11:00:00-06:00
confidence: 0.6
valid: false
superseded_by: 767c9affbfe80e533078e30ed03e9dd561f7ea212caf7db2a23f5e063b72f580
provenance:
  - event:1c2d3e4f50617283
  - event:2d3e4f5061728394
tags: [decisions, infrastructure]
refs:
  - "[[search]]"
---

The fictional observatory initially planned to use a third-party catalog lookup service.

## Observatory replaced the lookup service with embedded retrieval

---
id: 767c9affbfe80e533078e30ed03e9dd561f7ea212caf7db2a23f5e063b72f580
event_time:
  start: 2026-06-14
  end: null
ingest_time: 2026-05-27T11:05:00-06:00
confidence: 0.92
valid: true
provenance:
  - event:3e4f506172839405
  - event:4f50617283940516
tags: [decisions, infrastructure]
refs:
  - "[[search]]"
  - "[[sota-memory-and-recall]]"
---

The fictional observatory replaced its third-party catalog lookup service with an embedded local lookup engine.

## Training repository demonstrates documentation conventions

---
id: d5ab593e9323a8a51bc6babe368ff078ad4255698bae974ff5071200c87b85e8
event_time: 2026-05-17
ingest_time: 2026-05-28T09:00:00-06:00
confidence: 0.9
valid: true
provenance:
  - event:5061728394051627
tags: [juniper-observatory, infrastructure, decisions]
refs:
  - "[[training-project/architecture]]"
  - "[[training-project]]"
---

The fictional Juniper Observatory uses an invented training repository as its example for documentation conventions.

## Generated notebook holds invented star descriptions

---
id: 4a24429bd7955d2d22536164adc534e40f0b923a4a96b5b4cd65d57b98598792
event_time: 2026-05-18
ingest_time: 2026-05-28T10:00:00-06:00
confidence: 0.75
valid: true
provenance:
  - event:6172839405162738
tags: [infrastructure]
refs:
  - "[[generated-notebook]]"
---

The fictional observatory uses a generated two-megabyte Markdown notebook containing invented star descriptions as its ingestion source.
