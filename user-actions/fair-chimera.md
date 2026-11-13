---
slug: fair-chimera
title: Restart this host's runaway fseventsd
status: pending
category: infra
urgency: medium
blocks:
  - Prompt FSEvents delivery to the reindex daemon and the native watcher proofs on this host
filed: 2026-11-12
needed_at: now
source: kickoff
refs:
  - RELEASE.md
  - LOG.md
---

This is not done. On the release host, fseventsd (the macOS file-event daemon) has stayed at about 100% of one CPU core with roughly 26 to 28 GiB resident since at least 2026-11-12, far above its normal footprint. In that state single file events were delivered after several seconds, and some were not delivered within two minutes, while later events on the same stream arrived promptly. A public report describes the same runaway state on agent-heavy Macs, with no self-recovery and a reset after the daemon was terminated: anthropics/claude-code issue 98769.

Restarting it needs administrator rights and affects every process on the host that watches files, including the supervised Palace daemons, which reconnect on their own. One route the report gives is `sudo kill -TERM $(pgrep -x fseventsd)`; launchd respawns the daemon. After a restart, check its CPU and resident size with `ps -o pcpu=,rss= -p $(pgrep -x fseventsd)`, and confirm that the reindex daemon logs a change for an ordinary edit. Whether and when to do this is the operator's decision; no agent restarts system services.
