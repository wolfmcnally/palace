---
slug: magenta-rottweiler
title: Distinguish local role attempts from phase-wide review passes
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-10-21
source: kickoff
occurrences:
  - date: 2026-10-21
    ref: "Release 0.2.0 preparation PARK: first fresh-run role registrations used phase-wide pass 2"
---

As of 2026-10-21, the orchestrator preserved the phase-wide advisory allowance but mistakenly used its second-pass number as the first local role-attempt number in a fresh run. Registration and dispatch admitted attempt 2, the provider completed real independent reviews, and their exact reports were ingested. Later source validation required contiguous local attempts beginning at 1 and refused the run. The supported carry-advice route also validates its source, so a fresh run alone could not recover those reports. Immutable closed telemetry and cumulative launch budgets must not be rewritten to hide the error.

Local attempt identity and phase-wide review allowance are separate counters. Inspect both enforcing contracts before registration: a fresh run starts its local operation at attempt 1 even when the phase-wide launch is its second permitted advisory pass. A future explicitly approved machinery change could refuse a gap at registration before provider launch; this candidate lesson grants no checker change, budget reset, fabricated missing attempt or extra review.
