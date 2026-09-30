# Runtime operations

The machine store and human-readable vault are independent roots. `--store` or `PALACE_STORE` selects machine state (default `~/palace-data.noindex/`); `--vault-root` or `PALACE_VAULT_ROOT` selects Markdown facts and dreams (default `~/Obsidian/Palace/`). Select explicit disposable roots when trying the software.

## Processes and storage

| Process | Input | Output |
| --- | --- | --- |
| Capture | Loopback capture payloads | Store sessions and append-only events |
| Reindex | Configured watch roots, macOS FSEvents | Store change events |
| Index | Change events and source files | Rebuildable SQLite/FTS/vector index |
| Search | Selected indexes and inference selectors | Results; remote calls append store egress audits |
| Consolidate | Captures selected by source/day | Vault facts, dreams and contradiction records |

Consolidation is an on-demand cold-path operation. No built-in scheduler is installed; the operator decides when it runs. Capture, reindex and index are optional per-user macOS LaunchAgents. Synchronous indexing/search works without installing a service. Linux refuses macOS lifecycle operations.

## Optional macOS lifecycle

From the full checkout, inspect commands before installing a service:

```bash
./bin/python -m palace.cli capture --help
```

```bash
./bin/python -m palace.cli reindex --help
```

```bash
./bin/python -m palace.cli index --help
```

Each group exposes `install` and `uninstall`; group status wrappers live in `bin/palace-*-status`. Installation changes the user's LaunchAgents and may start a service. Choose roots and watch configuration deliberately; never install services as a packaging or test prerequisite. See the [daemon guide](../daemons/README.md) and individual capture, reindex and index guides for wire contracts, configuration and triage.

## Recall escalation

| Tier | Command | Result |
| --- | --- | --- |
| L1 | `palace search --store PATH "query"` | Ranked snippets |
| L2 | `palace search expand CHUNK_ID --store PATH` | Full section; optional neighbors |
| L3 | `palace search transcript SESSION_ID --store PATH` | Captured session JSONL |

Canonical content and indexes are not changed by recall. Remote L1 inference appends required egress audit records; local first-use reranking may provision model artifacts. L2/L3 perform no inference. A transcript can contain sensitive user content: restrict access to the selected store and do not publish it as a diagnostic fixture.

Use `--json` or `--verbose` to inspect chunk identifiers and each command's `--help` for its exact options. [Private providers](private-providers.md) explains authentication, authorization, audit and failure behavior. [Platform qualification](portability.md) defines tested scope; it does not promise production relevance or latency.
