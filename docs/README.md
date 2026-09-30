# docs/

Guides for users and library integrators. Start with the repository README and synthetic multi-store walkthrough; configure real inference only after selecting an explicit store.

- [`runtimes.md`](runtimes.md) — Operations guide: which palace process reads which directory, where each runs (capture / reindex / index LaunchAgents; consolidator on demand with no built-in scheduler), how to start/stop each, and the storage-unification audit confirming no process writes outside the two canonical roots.


- [Platform qualification](portability.md) — synchronous Linux scope, macOS refusal boundaries, hermetic fixtures and reproducible gate commands.

- [Multi-store retrieval](multi-store.md) — identity binding, origin, failure behavior and a fixture-only walkthrough.

- [Private inference providers](private-providers.md) — explicit boundary authorization, HTTPS adapter, audit, failure behavior and synthetic walkthrough.

- [Document metadata](metadata.md) — exact indexed string sets, document discovery without passages, atomic refreshes and constrained retrieval.
