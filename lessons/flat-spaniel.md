---
slug: flat-spaniel
title: Read the vendor contract for the exact API mode before calling platform behavior a fault
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Palace 0.5.0 native qualification diagnosis: missing FSEvents notices classified as a host fault and a latency norm quoted from memory, before Apple's documentation or upstream watchdog tests were read"
  - date: 2026-11-12
    ref: "Same diagnosis: Apple's 2012 directory-level guide applied to a stream created with kFSEventStreamCreateFlagFileEvents, and a product phase recommended on that inference"
---

As of 2026-11-12, after native watcher proofs failed, the primary concluded that the host's file-event daemon was faulty and stated a "normal" delivery latency from memory, without reading the vendor documentation or the upstream library's own tests. Apple documents no delivery bound ("inherently non-deterministic latency"), and upstream watchdog treats its two-second waits as robustness, not contract. When corrected, the primary then applied the legacy guide's directory-level semantics to a file-level stream mode and built a product recommendation on it; the current documentation for that mode states no per-file guarantee either way.

Before classifying an operating-system or library behavior as a fault, or quoting a performance norm, read the vendor's current contract for the exact mode in use: check the installed library's actual flags first. Read the upstream project's own test practice too, and cite both. A local experiment shows what happened, not what is guaranteed. Mark any claim that is not sourced as unsupported. This is an unratified candidate.
