# MEMORY

All people, projects, deployments, and operational events below are fictional test data.

Durable bitemporal facts for the consolidate test fixtures, per `policies/fact-schema.md`. Two facts are load-bearing for the suite: the `master`-default-branch fact (the contradiction case targets it) and the prose-first fact-schema fact (the novelty case restates it).

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

## Observatory stores calibration facts as prose with frontmatter

---
id: 0293a5d4a69f5f76a1c08b194fca6ef008930cdfcdde080dc31bb1d322d1c056
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
---

The fictional observatory stores calibration facts as natural-language Markdown sections with YAML frontmatter rather than graph triples.

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
