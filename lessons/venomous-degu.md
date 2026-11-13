---
slug: venomous-degu
title: Run a recipe's own commands in the recipe's order
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 29 planning: mark-plan-reviewed ran before ingest-findings, and ingest then refused the report"
  - date: 2026-11-11
    ref: "Phase 29 focused checks: a hand-built ruff call over a shell script reported 72 spurious errors; the gate wrapper's lint target passed"
  - date: 2026-11-12
    ref: "Phase 25 dogfood fresh run, attempt 1: a gate review recorded 'every policy check passes' for a policy gate that had exited 1, because the review was issued in the same command that first printed the gate result; superseded by an accurate review"
  - date: 2026-11-12
    ref: "Phase 25 superseded run close: span finishes guessed outcome 'failure' instead of the tool's documented vocabulary, so two spans were left for the close to mark interrupted"
---

As of 2026-11-11, two slips in one phase came from departing from the written recipe rather than from a defect in the tools. The planning resource says to ingest the reviewer's report and then mark the plan reviewed; the primary ran the two in the opposite order in one shell line, so the plan was marked reviewed while its review was refused at ingest. Later the primary invoked ruff by hand on a path list that included a shell script, got 72 parse errors, and briefly reported them before running `./bin/check lint`, whose own target list passed.

Run the recipe's commands one stage at a time, in the documented order, and read each result before the next command. For a gate's tool, use the gate's own wrapper and target list instead of a hand-built invocation. This is an unratified candidate.

As of 2026-11-12 the same departure recurred twice. A review summary was written in the same command that first printed the gate result, so a failed gate was recorded as passing until it was superseded. Separately, a span outcome value was guessed instead of read from the tool's help. In both cases the result should have been read before the next step.
