---
slug: observant-robin
title: Bound aggregate request context independently of item count
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-19
source: kickoff
occurrences:
  - date: 2026-10-19
    ref: "Remote embedding request-budget correction PARK: synthetic multi-section capacity regression"
---

An embedding request can remain below its provider's item-count cap while exceeding aggregate context. The existing 1024-input splitter admitted a synthetic 167-section production plan as one request containing 448175 NFC byte symbols. The new transport-capacity witness rejected that request before accepting the bounded implementation.

Qualify both cardinality and aggregate text size with realistic multi-section inputs, preserving exact input/vector order and per-request provenance. A conservative byte-level tokenizer bound must account for the model's normalization and framing: Qwen's NFC normalization can expand some Unicode text. The correction sizes the larger raw or NFC UTF-8 representation plus framing without modifying sent text. Long compressible inputs may be refused conservatively; that is an explicit limit, not proof that their actual token count exceeds model capacity. Independent review and both full gates remain pending at this park. This is one local candidate lesson, not a graduated rule.

Review continuation: independent advice confirmed the tokenizer bound and identified missing durable provider provenance plus an unproved raw-byte reserve. Dated relayed provider observations and their limits are now recorded in the index README; a decomposed-accent boundary proves the raw-byte term, and batch progress is local by construction. The reconciled candidate passed the complete implementation gate. Report generation, the handoff gate and delivery remain pending at this note; no lesson graduation is claimed.

Bounded-close clarification: the operator identified this repair as a bounded fix outside newly commissioned phase work. Independent review and the full implementation gate remain valid; the recorded report refusal is preserved as evidence of the agent's unsupported phase binding. The final unchanged handoff gate and guarded delivery remain required. Live qualification relayed by its owner reproduced the intended successful batching on the original larger-page case; no source drift mechanism was established.
