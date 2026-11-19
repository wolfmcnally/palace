---
slug: educational-mouflon
title: Prove a resource bound against an independent measurement, not the implementation's own ledger
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-19
source: kickoff
occurrences:
  - date: 2026-11-19
    ref: "Phase 35: the vector cache's memory proof sampled the cache's own byte ledger, so working buffers the ledger never reserved (a whole fetched feed interval, filtered id lists, scorer copies, a compaction gather) were invisible to it; the independent code advisory found them (CODE-F005), and a tracemalloc measurement then caught two more the first fix still missed (a load's range-check arrays, an over-sized query reservation); the documented block size was also stated as 32 MiB when 8,192 x 4,096 x 4 bytes is 128 MiB"
---

As of 2026-11-19, a proof that a component stays within a memory budget read the same counters the component maintains. Such a proof can only confirm the ledger is internally consistent: an allocation the ledger never records cannot make it fail. Measuring the same operations with an oracle the implementation does not control (tracemalloc for Python and numpy allocations) and asserting the measured peak is within the reserved amount plus a stated slack exposed omissions the self-consistent ledger hid. The same applies to stated sizes: derive them arithmetically in the document or comment (rows x dimensions x bytes) rather than asserting a figure. This is an unratified candidate.
