# Changelog

## 0.1.0 — unreleased

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
