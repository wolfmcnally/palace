---
slug: celestial-oryx
title: Keep helper scripts named after standard-library modules out of a qualification script's directory
status: candidate
scope: local
proposed_surface: bin
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 22 bearer correction 0.4.1 package qualification"
  - date: 2026-11-12
    ref: "Palace 0.5.0 dashboard diagnosis: a scratch diff helper run from the job temporary directory failed importing tempfile because a stale bisect.py there shadowed the standard library"
---

As of 2026-11-11, the first macOS package qualification attempt failed with an `email.utils` circular-import error inside a freshly created virtual environment. The cause was an evidence script named `bisect.py`, copied into the same scratch directory the qualification script uses as the working directory for its `python -c` checks; Python put that directory first on `sys.path`, so the helper shadowed the standard-library `bisect` module. Nothing was wrong with the package. Moving the helpers into a subdirectory and rerunning in place passed, and the failed attempt's outputs were preserved separately.

Give scratch helper scripts names that cannot collide with standard-library modules, keep them out of any directory used as a Python working directory, and check `sys.stdlib_module_names` against the directory before launching a qualification run.

As of 2026-11-12 the same shadowing recurred with a scratch diagnostic run from a shared job temporary directory that still held the earlier bisect.py. It was rerun from an isolated directory.
