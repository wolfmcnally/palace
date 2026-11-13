---
slug: copper-quail
title: Check the light-lane exclusion list against the deliverables before declaring a lane
status: candidate
scope: local
proposed_surface: policy
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 32: the primary authored the phase file with review_lane light for a parser behavior change and a new shared query helper; the code critic found both on the light-lane exclusion list (input parsing, user-visible behavior), forcing a park and a full-lane rerun with retroactive plan review"
---

As of 2026-11-12, the primary declared the light review lane for Phase 32 because the change was small. The light lane requires that the work is mechanical and that none of the exclusions appear; input parsing, new public surface and user-visible behavior changes are all excluded, and this phase had all three. Size is not the test. The plan review that the light lane omits is the lane's first lane-fit check, so a wrong declaration by the phase author is caught only at code critique, after implementation.

When authoring a phase file, read the light-lane eligibility lists in `policies/review-lanes.md` and check each deliverable against them before writing `review_lane: light`; when borderline, declare full. This is an unratified candidate.
