# Changelog

## 0.2.0 — 2026-10-21

- Fair, bounded synchronous indexing across large and small documents, durable source- and embedding-identity-bound receipts, atomic completed-document visibility, and structured progress. Existing chunk text, embedding inputs and provenance remain unchanged.
- Declared admission limits visibly refuse oversized sources, tails, plans and chunks before inference. The Python API exports `BuildLimits` and `BuildProgress`; the former soft chunk-window constant is removed. These changed pre-1.0 contracts require a minor release. Limits are engineering choices, not measured throughput, memory or cost guarantees.
- Interrupted builds reuse compatible work. A changed frozen source or corrupt receipt refuses with a recovery remedy; no automatic corrupt-receipt deletion or online embedding-generation swap is provided.
- The indexing daemon parks at its first failed event and retains the last successful checkpoint, so later events cannot move the cursor past a failed update. Remedy and explicit restart remain operator actions.
- Remote embedding requests respect the conservative aggregate request budget, while preserving request order, input bytes, audit and provenance semantics.
- Test-only native watcher proof hardening and storage error propagation corrections, plus clearer project introduction.
- Upgrading from 0.1.0: the default ordinary-source/unconsumed-JSONL-tail limit is 8 MiB, with 16,384 changed chunks per document, a 64 MiB serialized plan, 100,000 files per invocation and 1 GiB logical staging. One over-limit source refuses the whole synchronous build before inference, including deletion reconciliation and identity certification; a JSONL file over 8 MiB also refuses `--full` because it starts at offset zero. Single inputs over 128 KiB raw now refuse; OpenRouter inputs must additionally fit its 30,000 conservative normalized-byte/framing-unit budget. The CLI has no limit flags: narrow or ignore sources, or deliberately adjust `BuildLimits` through Python. Existing rows remain visible on admission refusal.
- Callers parsing `embed_batching` must handle the new `per-turn(...)` labels. Local and private HTTPS builds use at most 32 inputs per turn; OpenRouter uses at most 1,024 under its request budget. Derived receipts live in `index/build-staging`; daemon state can report `park_kind: work-failure`. The Python `build_root` entry point requires a file-backed database.
- Synthetic upgrade qualification with an injected deterministic embedder: a store created by the published 0.1.0 wheel remains searchable and its unchanged incremental build is a no-op under 0.2.0; canonical source bytes are preserved. Real provider identities and operator stores are not certified.
- Clean exact-wheel installation and synthetic synchronous indexing/retrieval are qualified on macOS ARM64 and Linux ARM64. Package-registry publication, production inference, Linux daemons and other architectures remain separate.

## 0.1.0 — 2026-09-30

- Published vector indexes: a store opted in as published publishes its chunk vectors to an Amazon S3 Vectors index incrementally by chunk key (puts before deletes, a receipt that avoids listing the index), and can select that index as its vector search leg; every S3 Vectors request is audited and counted.
- Per-file store operations: `palace index copy` moves named files' rows between stores without embedding, `palace index remove` deletes named files, and a keyword-row lookup lets every per-file operation find a file's keyword rows without reading the whole keyword table.
- Concurrent index writers: a per-store writer lock the operating system releases with its holder, taken by builds, the daemon, per-file updates and metadata batches; stale plans are re-planned rather than committed; `palace index update` reflects named files without walking the tree.
- Exact indexed document metadata, atomic replacement/deletion, paginated document lookup and metadata-constrained retrieval without corpus reembedding.
- Local-first file/event memory substrate with rebuildable lexical and vector retrieval.
- Explicit store indexing and complete embedding identity checks.
- Origin-preserving multi-store retrieval with one merged rerank.
- Explicitly authorized private HTTPS embedding and reranking, credential references, bounded requests, audit and no provider substitution.
- Synchronous macOS ARM64 and Linux ARM64 qualification; macOS-only watcher and service boundaries remain explicit.
- MIT release preparation, scoped archives and clean-install verification. Publication and stable compatibility remain owner decisions.

Personal retrieval evaluation, additional private/cloud adapters and production deployment qualification remain separate work.
