---
slug: accelerated-swine
title: Report clock times only from an observed clock or file timestamp
status: candidate
scope: local
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 27 coordinator reports carried estimated times: 18:56Z for a message sent at 18:51:16Z, and about 18:52Z for a review result written at 18:49:56Z"
  - date: 2026-11-11
    ref: "Phase 28 opening: two prepared coordinator reports carried digit placeholders for unread minutes or seconds; both were caught before submission and re-prepared with observed bounds or the envelope's own sent_at"
---

As of 2026-11-11, under a standing instruction to report observed times, two coordinator reports in one hour carried times the primary had estimated rather than read. One header said 18:56Z for a message whose envelope recorded 18:51:16Z. One completion was given as "about 18:52Z" for a result file last written at 18:49:56Z. A similar correction had already been needed earlier the same day. Each was corrected in a follow-up message, but a coordinator schedules other owners' test slots from these times, so an estimate can misallocate shared resources.

Before writing any clock time into a report, read it in the same step from `date -u`, an envelope's sent_at, or a file's modification time. Label anything else as an estimate. This is an unratified candidate.

The second occurrence shows the prevention point is the drafting step, not only the send: a placeholder such as a trailing "x" in a timestamp was written while composing, before any clock was read. Re-reading the prepared envelope before submission caught both. Writing "this envelope's sent_at" or an observed before-and-after bound avoids inventing a time at all.
