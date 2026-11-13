---
slug: puzzling-oryx
title: Convert a third-party loader's leaked exceptions at the load boundary instead of enumerating them
status: candidate
scope: local
proposed_surface: policy
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 32: PyYAML's safe loader leaked ValueError and RecursionError (first fix), then KeyError, IndexError, AttributeError and OverflowError (first code critique), then TypeError (retroactive plan review); each enumeration was incomplete until the load call alone was wrapped and every Exception except MemoryError converted"
---

As of 2026-11-12, PyYAML 6.0.3's `yaml.safe_load` reports most rejections as `yaml.YAMLError` but lets Python's own exceptions escape from its constructors for ordinary input. Three successive attempts to list those classes from probes were each incomplete, and each gap was found only by a separate review pass.

When a pure third-party parse call on caller-supplied text must map every failure to one contract ("cannot be loaded"), wrap only that call, convert every `Exception` except resource exhaustion into one named error with the original as its cause, and keep the caller's own code outside the handler. Enumerate leaked classes only as test fixtures. This is an unratified candidate.
