---
title: Declare a gate artifact only when the gated command writes that file
scope: local
proposed_surface: skill
source: kickoff
slug: cyan-velociraptor
status: candidate
filed: '2026-11-21'
occurrences:
- date: '2026-11-21'
  ref: 'distinct-source-results acceptance: baseline-parity gate attempt 1 recorded
    with no artifact digest; superseding manifest 9709841b and attempt 2'
---

As of 2026-11-21, the baseline-parity acceptance gate was run through `kickoff-evidence run-gate --artifact <path>` on the assumption that the tool would capture the command's output there. `--artifact` instead names a file the gated command itself must write; the script wrote none, so the gate was recorded with `artifact_required` and no digest, and the tool exited 2 although the command passed. Recovering cost a superseding gate manifest, a second command zero and a rerun.

Run-gate already records the command's diagnostics digest. Pass `--artifact` only for a command that writes the named file, and check a new gate's flags against `run-gate --help` before the first expensive run. This is an unratified candidate.