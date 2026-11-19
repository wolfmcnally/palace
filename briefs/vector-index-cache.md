---
title: "Optional RAM-resident vector acceleration"
date: 2026-11-19
status: draft
scope: "Design and measured spike for an opt-in, embeddable in-memory accelerator of the vector leg, judged against the released exhaustive SQLite search on speed at comparable recall, memory and operating burden; synthetic vectors only, no consumer inventory or deployment evidence."
---

# Optional RAM-resident vector acceleration

As of 2026-11-19, against Palace v0.6.0 (tag commit `112a582af016840f8055ac97d2dadce7435391dd`) with sqlite-vec 0.1.9. This brief records a design for serving the vector leg of search from memory, the alternatives that were weighed, and a reproducible spike that measured them. It is a design-and-spike gate: nothing here is implemented in Palace, and the released exhaustive search stays the default whatever is decided. Every number below was measured on one shared Apple-silicon workstation (18 cores, 64 GiB) that was busy with unrelated work throughout; baseline and candidates were interleaved in repeated rounds so that a claimed gain has to stand clear of that noise.

## Recommendation

**Go to the next gate for an opt-in, append-only RAM accelerator. Do not adopt a graph index. Take the memory-map setting on its own.** The reasons, most decisive first:

1. **Where it applies, the gain is large, repeatable and clear of the noise.** At 100,000 rows per index a search through Palace's own API went from 233 ms to 22 ms with results identical to the released path, and to 11 ms with the compact scan at recall@50 of 0.995. Under eight concurrent queries the released path served 2.6 searches per second and the accelerated paths 125 and 187. Comparing the worst accelerated round with the best released round of the same configuration, the exact engine won by at least 5.5× for a single query stream and 9× under two or more concurrent queries, and the compact scan by at least 10× and 18×. At 1,000,000 rows the search went from 2.05 s to 155 ms exact or 50 to 70 ms with the compact scan, and eight concurrent queries from 30 s each to 0.14 to 0.21 s each.
2. **It met the concurrency and update requirements together, which no alternative did.** Across 4,573 probe queries during ongoing inserts, deletes and edits, no query that started after a commit missed it and none saw half of one. A query pins its view for its whole life, including a query over several stores, and behaves exactly as the released path does when a commit lands mid-query. No update copied an index: a change appends a row and replaces one small stamp array.
3. **Its price is memory, and it is not small.** One copy of the vectors per process: 17 to 18 KB per chunk at 4096 dimensions as measured, so 1.78 GB per 100,000 chunks and 17.2 GB per million. A float16 copy halves that at the same recall in a standalone test. The memory budget has to cover the stores in use; under-sized, it thrashes with second-long tails.
4. **Its administrative price is a change feed and a version-bound loader.** Palace's writers must append every vector change to a small table, and deletes must clear it. The fast loader reads sqlite-vec's internal storage tables and has to be re-qualified when sqlite-vec changes; the supported route is more than 100 times slower. It adds no dependency, no daemon, no index file and no rebuild job.
5. **It is not worth it for small stores or short-lived processes.** Palace's fixed work per search is about 5 ms, so a store of a few thousand rows has little to gain, and a process-local cache does nothing for a one-shot command.
6. **The memory-map setting is nearly free and should be decided separately.** One line on the read connection gave 1.7× for a single query stream and 7.5× at eight threads at 100,000 rows, with no index, no budget and no change in results. It stops helping once the database exceeds what the SQLite build will map (about 2 GiB here), and the accelerator is still 6 to 10 times faster where both apply.
7. **A graph index does not earn its cost here, and a graph updated in place is disqualified.** At 100,000 rows HNSW needed 15.5 ms to reach the recall the compact scan reached in 5 ms, took 107 s to build and held a second copy of the vectors. At 1,000,000 rows its build was stopped unfinished after 23 minutes because its 34 GB had put the shared machine into memory compression, so there is no result at that size. Updating a serving graph in place under the only locking its documentation permits cut reads to 2.0 per second at 200 changes per second. Rebuilding whole generations needs a second full copy in memory and minutes per publication.
8. **Sequence.** Build the exact in-RAM engine first: it changes no result, so it does not need the representative evaluation that Palace requires before a retrieval change. The compact scan follows that evaluation. The review items listed below come before either.

Not established by this spike: anything on Linux or inside a confined environment, recall on real data, hybrid and reranked searches, and scaling beyond one process. These are listed under "Not measured".

## What was asked

The operator's requirements, as stated on 2026-11-19, in generic terms:

1. The question is whether the gain in search speed outweighs the memory and the administrative overhead. If a gain cannot be told apart from ordinary machine noise it is not worth adopting.
2. A host process must be able to run many concurrent queries against several RAM-resident indexes, including indexes that are updated over time. This is the primary acceptance gate.
3. A query runs against a stable set of index versions: every selected index version is pinned before retrieval and held until the query completes. The next query may see newer versions of some of them. Indexes publish independently; no cross-index transaction is required.
4. Ordinary updates must not duplicate an index in memory. Updating happens constantly while a corpus is ingested.
5. The whole optimization is optional. The exhaustive SQLite query remains the available baseline, and a store configured for it needs no index build, no warm-up and no optional dependency.
6. Whether to update a serving index in place while queries run, or to use another strategy, is a question to answer by comparison, not a premise.
7. Compatibility criteria carried over from the original request: embeddable in a host process with no mandatory daemon; a caller's per-store authorization applies to every query and every cache hit; metadata filters and deletions stay exact; nothing new and unencrypted is written at rest, so a store kept on an encrypted volume inside a qualified boundary stays inside it.

## What v0.6.0 does today

Read from the released source, not inferred:

- **Storage.** `palace/index/schema.py` declares `chunks_vec` as `vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[dim])` at the store's identity dimension (4096 for the default local model). No distance metric is declared, so sqlite-vec's default applies: L2. The table is flat; sqlite-vec stores 1,024 vectors per storage blob.
- **Search.** Every search opens a fresh read-only connection, loads the extension, begins a read transaction, runs the admission checks and then the vector leg (`palace/search.py`, `_open_chunks_db_ro`, `_admit_store`, `_vector_search`). The vector leg is one exhaustive KNN statement, `WHERE embedding MATCH ? AND k = ?`, with a metadata filter added as `chunk_id IN (subquery)`. The default candidate pool is 50. Nothing is kept between searches.
- **Several stores.** `palace/multistore.py` opens and reads every named store before any retrieval (`_admit_stores`), so each store's SQLite snapshot is already pinned when retrieval starts; stores are then searched one after another on those snapshots.
- **Writers.** Writers hold an advisory per-store lock (`palace/writer_lock.py`) and one `BEGIN IMMEDIATE` transaction. A vector is never updated in place: a changed chunk is deleted and inserted. The database runs in WAL mode with `synchronous=NORMAL`.
- **No change feed.** Nothing in the store records which vector rows changed. `read_data_version` exists and can tell that something committed, not what.
- **Dependencies.** numpy is already installed with Palace (through onnxruntime) and already imported by `palace/rerank.py`.

Measured facts about the released path that shape the design:

- **The scan is linear and slow at this width.** One vector-mode search through Palace's own API took 29–39 ms at 10,000 rows and 233 ms at 100,000 (median; the KNN statement alone on a held connection took 221 ms).
- **It does not scale with concurrency; it collapses.** At 100,000 rows one worker completed 3.7 searches per second and eight workers together completed 2.7, the same whether the workers were threads of one process or separate processes. In the load runs the median search time went from 241 ms with one thread to 3.1 s with eight and 6.8 s with sixteen.
- **A cheap setting recovers part of that with no index at all.** Asking SQLite to memory-map the database on the read connection (`PRAGMA mmap_size`) took the single search from 241 ms to 140 ms and eight-thread throughput from 2.6 to 19.6 searches per second. This Python's SQLite caps the mapping at about 2 GiB, which is roughly 120,000 rows at 4096 dimensions, so a larger store is only partly mapped: at 1,000,000 rows the setting changed almost nothing (1.92–1.97 s against 2.05–2.06 s). It is measured below as its own alternative, because it costs no memory beyond the page cache and no administration.
- **Palace's fixed work per search is about 5 ms** (fresh connection, extension load, admission, hydration): with the vector leg answered in 2 ms the whole search took 8 ms. No accelerator can take a search below that floor without changing those parts, so a small store has little to gain.
- **Single vectors cannot be read back cheaply.** Fetching one stored vector by chunk id costs about 1.7 ms, and a full pass through sqlite-vec's SQL interface loads 600–1,200 rows per second (10,000 rows in 8–16 s over three runs). Reading sqlite-vec's storage tables directly loads 130,000–210,000 rows per second, at the price of depending on that version's internal layout. Any design that rescores candidates against exact vectors must therefore hold those vectors itself.
- **A filter does not make the scan cheaper.** With a metadata filter admitting 0.1 % of rows the search still took 21 ms of the unfiltered 29 ms at 10,000 rows.
- **A known defect is confirmed in v0.6.0.** A filtered vector search returns no rows at all, without an error, when any chunk matching the filter has no vector row. The accelerated path returns the correct rows. This defect is independent of this brief and wants its own fix.

## Design

### Position

SQLite stays the authority. The accelerator is derived state: a process-local, in-memory copy of one store's vectors arranged for fast candidate generation. It is never the only copy of anything, it is rebuilt from the store by one pass, and by default nothing of it is written to disk. A host process that wants it constructs one accelerator cache and passes it to the search entry points; a process that does not is running exactly today's code. There is no daemon and no global state.

### Structure

One store's accelerator is an append-only sequence of fixed-size blocks. A block holds, per row: the float32 vector, a 1-bit sign code of it (512 bytes at 4096 dimensions), its squared norm, its chunk id, and two change-sequence stamps, `born` and `dead`. Published rows are never rewritten. A *view* is an immutable value: the tuple of blocks, the row count, and one `dead` array per block. A row is visible to a query at change sequence S when `born <= S < dead`.

Two consequences follow from this shape, and both were the point of choosing it:

- **Stable query views without locks.** A query takes the current view once and uses it to the end. Later appends land beyond its row count; later deletions replace a block's `dead` array in a newer view and leave the query's own array untouched. Readers take no lock and read only memory that no writer will touch again.
- **No index duplication for ordinary updates.** An insert appends one row. A delete copies one block's stamp array (8 bytes per row of that block: 64 KiB for the 8,192-row blocks used in the spike) and writes one stamp. The vectors and codes of every unchanged block are shared by every view. Memory that an old view alone holds is exactly the replaced stamp arrays, and it is released when that query ends.

### Change feed

The accelerator has to learn what committed. The design adds one small table to the store, appended by the writer inside the same transaction as its vector rows: a monotonically increasing sequence, an operation (insert or delete), the chunk id and, for an insert, the vector. A reader applies the entries its own snapshot can see. The table is pruned to a bounded tail; an accelerator that has fallen behind the tail reloads.

Carrying the vector in the feed is a measured choice, not a convenience. Reading a new vector back through sqlite-vec costs about 1.7 ms per row; reading it from the feed costs about 0.006 ms per row (1,000 vectors: 1.66–1.75 s against 5–7 ms, three rounds). Inserting one chunk with Palace's own statements took about 1.5 ms, about 650 rows per second per store, so a feed of ids alone could not keep an accelerator current during ingestion. The cost of carrying it is disk, not time: writer time per row was indistinguishable with and without the feed (1.52–1.63 ms against 1.52–1.65 ms), and each logged insert holds one extra copy of its vector (16 KiB at 4096 dimensions) until pruned. That copy is a new place a deleted vector can linger, which the deletion section below addresses.

### Query protocol and the visibility contract

For each store a query touches, in this order: Palace admits the store and pins its SQLite snapshot (as today); the caller's authorization hook runs; the accelerator applies every feed entry visible in that snapshot and notes the snapshot's sequence S; the query pins the current view; candidates are generated among rows visible at S; the best candidates are rescored with exact float32 arithmetic and returned.

The contract this gives, stated so it can be tested:

- **Read-after-commit.** A query that starts after a commit has returned observes that commit. There is no staleness window and no background lag a query can fall into, because the query itself brings the index up to its own snapshot before searching. A background refresher is an optimization that moves this work off the query path; it cannot weaken the contract.
- **In-flight queries.** A query that pinned its snapshot before a commit answers from that snapshot, exactly as the released path does under SQLite's snapshot isolation: a row deleted mid-query may still be returned by that query and by no later one.
- **Several stores.** Each store's snapshot is pinned at admission, before any retrieval, and the accelerator answers each store at its own pinned sequence even if that store's index has since moved on. Stores never need to publish together.
- **Coherence.** A change that deletes one row and inserts its successor in one transaction is seen whole or not at all.

### Filters, deletion and excision

A metadata filter is evaluated by Palace's existing subquery in the query's own snapshot; the resulting chunk ids become an exact allow-list applied before candidates are chosen, so a selective filter costs less, not more, and can never admit a row outside it. Ids with no vector row are simply absent.

Deletion keeps today's meaning: invisible to every later query from the commit onward. Two residues need a decision at implementation time and are named here rather than hidden. First, a deleted row's bytes stay in the accelerator's memory until its block is compacted or the store's accelerator is evicted (measured: still present after the delete was applied; gone after eviction). Evicting a store's accelerator is always available, costs one reload, and is the conservative answer to an excision. Second, the change feed holds inserted vectors on disk until pruned; a delete must clear the vector from earlier feed entries for the same chunk in the same transaction, and a store with a remanence requirement must run with SQLite's `secure_delete` so freed pages are overwritten. The released vector table already overwrites a deleted slot in place; the feed must not be a regression against that.

### Engines

Three ways of generating candidates share the structure above:

- **Exact in RAM.** A brute-force float32 scan of the blocks. Recall is 1.0 by construction.
- **Compact scan with exact rescoring.** A popcount scan of the 1-bit codes selects the best R rows, which are rescored with their float32 vectors. The only approximation is which rows reach rescoring; returned distances are exact. It needs nothing beyond numpy.
- **Graph segments.** An HNSW graph over a sealed prefix of the rows, with the unsealed tail scanned exactly. A graph is only ever built privately over rows that are already immutable and then published whole; it is never mutated while served.

### Cache, memory budget and authorization

The cache maps explicitly named stores to accelerators under a byte budget with least-recently-used eviction. Loads are single-flight (one load per store, concurrent queries for it wait on that load) and bounded in number. The cache never searches a store a query did not name and has no operation that enumerates its contents on behalf of a query.

The caller's authorization hook is asked for every store on every query, before the cache is consulted, whether or not that store's accelerator is resident. A refusal fails the query and, by default, evicts the store's accelerator. Being cached is never a reason to answer. The prototype asks after Palace has admitted the store and before it touches the cache; an implementation should ask before admission as well, so a refused query opens nothing.

### Configuration and failure behaviour

Acceleration is off unless a host process constructs a cache and a store's configuration selects it. The default, exact mode, is the released code path and imports nothing new.

What an accelerated query does when the accelerator cannot serve it (a load fails, the budget cannot hold the store, the storage self-check refuses, the feed has been pruned past it) is a decision for the operator and is deliberately not inferred here. The two candidates are: refuse the query with a named error, or answer from the exhaustive SQLite path and say so in the result. Both return correct rows; they differ in whether a latency cliff is visible or silent. The recommendation is to make it an explicit three-valued setting (exact; accelerated, refuse on failure; accelerated, fall back and report) with no default beyond exact.

### Stores inside an encryption boundary

Because the accelerator is process memory and the default design writes no index file, a store that lives on an encrypted volume gains no plaintext artifact outside it: the only new thing at rest is the change feed, inside the same database file. A process that holds accelerators for several such stores holds their vectors in its own memory for as long as they are resident, which is a property the embedding application must be designed for; the per-query authorization hook and eviction on refusal are what Palace offers it. A memory-mapped sidecar for the float32 rows was measured as an alternative (same speed as anonymous memory) and is not recommended as a default, because it is a second plaintext copy of the vectors at rest.

## Update strategies compared

The question was whether to update a serving index incrementally while queries run, or to do something else. Two things have to be kept apart to answer it: *maintaining an index incrementally* (each change costs work proportional to the change) and *mutating the instance that queries are reading*. The design above does the first and never the second. Three strategies were screened at 100,000 rows with eight reader threads, each query being the index search plus 4 ms of other query work during which its view must stay pinned (Palace's own fixed per-query work is of that order). Readers per second, median of each run; ranges are the two rounds where a strategy ran twice. The graph rows ran at a search breadth that gave recall@50 of 0.957 and the compact-scan rows at 0.993, so this table compares strategies, not engines; engines are compared at equal recall in the spike section.

| Strategy | Read throughput at 0 / 20 / 200 changes per second | Read p95 at 0 / 20 / 200, ms | Cost of one change | Extra memory while updating | Freshness |
|---|---|---|---|---|---|
| In place: one mutable HNSW graph, shared lock held for the whole query, exclusive lock per change | 586–614 / 368–454 / 2.0 | 16–18 / 34–41 / 8,006–8,040 | 8–10 ms holding the exclusive lock (p95 14–17 ms) | none | immediate, when the writer gets the lock |
| Immutable generations: rebuild the whole graph in the background, publish by one reference swap | 637–680 quiet; 602–659 while rebuilding | 14–15 quiet; 15–17 while rebuilding | a full rebuild: 179–234 s for 100,000 rows on four threads | one whole extra index (3.39 GB to 5.07 GB peak) | no fresher than one rebuild, minutes |
| Immutable base, append-only tail and stamps, graph over the sealed rows | 587 / 589 / 581 | 16.7 / 16.7 / 16.8 | 0.06–0.09 ms | the appended rows plus one 64 KiB stamp array per touched block | immediate |
| The same structure with the compact scan instead of a graph | 368 / 337 / 341 | 26 / 29 / 29 | 0.14 ms | as above | immediate |
| The same structure with the exact scan | 105 / 105 / 104 | 89 / 88 / 90 | 0.08–0.10 ms | as above | immediate |

What this shows:

- **In-place mutation is disqualified.** hnswlib's own documentation permits concurrent searches and concurrent adds but not a search concurrent with an add, so a correct in-place design must exclude one from the other, and a stable whole-query view means the shared lock is held for the whole query. Each insert then holds the exclusive lock for about 8–10 ms at this dimension. At 20 changes per second read p95 doubled; at 200 per second the lock was held almost continuously, the writer itself fell behind (883–1,091 changes applied of 1,600 offered) and readers were served at 2.0 queries per second with a median of 4.0 s. Both rounds agree. Batching changes under one hold does not reduce the total time the lock is held. No unsupported overlap was ever exercised to obtain these numbers.
- **Whole-index generations break two requirements at once.** Readers are untouched, but every publication costs one full extra copy of the index in memory and cannot be fresher than a rebuild that takes minutes at 100,000 rows. It would need a delta and tombstones bolted on to meet the freshness contract, at which point it is the third strategy with a larger merge.
- **Base plus append-only tail meets every requirement measured.** Readers did not slow at any write rate tested, a change costs a fraction of a millisecond, and no update copied an index. With a graph, the price is the tail: single-thread search went from 7.2 ms with 1,000 unsealed rows to 10.7 ms with 16,000, and sealing those 16,000 rows into a second segment took 17.5 s on six threads. With the compact or exact scan there is no tail and nothing to seal or merge, ever; the whole index is the append-only structure.

Reclaiming the space of deleted rows is the one periodic cost the recommended structure keeps. It is bounded by design to rewriting one block at a time, so its transient memory is one block (134 MB of float32 at 4096 dimensions and 8,192 rows), not one index. It was designed, not built or measured, in this spike.

## Spike

### Method

Stores were created by Palace v0.6.0's own `init_db` at the default 4096-dimension identity and filled with the statements its writer uses. Vectors are seeded synthetic unit-norm sets of 10,000, 100,000 and 1,000,000 rows, plus six 100,000-row stores cut from the million. Queries are 200 held-out vectors per set; ground truth is exact float64 L2. k is 50, Palace's default pool. Searches go through Palace's own entry points (`execute_prepared_search`, and `execute_prepared` for several stores), so every number includes the fresh connection, admission and hydration that a real search pays; embedding is excluded because queries were prepared in advance. Four serving paths were compared, interleaved round by round: the released path; the released path with the memory-map setting; the accelerated leg with the exact engine; and the accelerated leg with the compact scan rescoring 800 candidates. A fixed recall target was declared before the load runs: recall@50 of at least 0.99 against exact, which the compact scan meets at 800 candidates at 100,000 rows (0.995) and the exact engine meets by construction. At 1,000,000 rows the compact scan needs 1,600 candidates for the same target, so the 1M load figures are given at both depths.

In the tables `a (b–c)` is the median and the range over the interleaved rounds. The machine was never quiet; the last column of each load table compares the worst accelerated round with the best baseline round, which is the fairest reading of whether a gain clears the noise.

### One index, one thread, the vector leg alone

Milliseconds, median (p95). Recall@50 after rescoring; for the compact scan the candidate recall before rescoring is the same number, because rescoring is exact.

| Engine | 10,000 rows | recall@50 | 100,000 rows | recall@50 | 1,000,000 rows | recall@50 |
|---|---|---|---|---|---|---|
| Released SQLite scan (statement on a held connection) | 21.6 (22.3) | 1.000 | 221 (235) | 1.000 | 2,012 (2,071) | 1.000 |
| Exact scan in RAM | 1.6 (1.7) | 1.000 | 15.1 (16.4) | 1.000 | 146 (154) | 1.000 |
| Compact scan, rescore 400 | 0.84 (0.89) | 0.997 | 4.7 (4.8) | 0.977 | 43.5 (44.1) | 0.935 |
| Compact scan, rescore 800 | 1.15 (1.23) | 1.000 | 5.1 (5.2) | 0.995 | 46.8 (54.2) | 0.974 |
| Compact scan, rescore 1,600 | 1.77 (1.93) | 1.000 | 5.7 (6.0) | 0.999 | 46.8 (47.4) | 0.991 |
| Compact scan, rescore 3,200 | 2.84 (3.12) | 1.000 | 7.2 (7.5) | 1.000 | 49.6 (50.7) | 0.998 |
| HNSW (M 16), ef 200 | 3.6 (5.0) | 0.997 | 5.7 (7.8) | 0.974 | not measured |  |
| HNSW, ef 400 | 5.8 (7.8) | 1.000 | 9.2 (13.3) | 0.988 | not measured |  |
| HNSW, ef 800 | 9.2 (11.9) | 1.000 | 15.5 (22.6) | 0.997 | not measured |  |

The work behind those numbers is explicit and not hidden behind a cap: the compact scan reads every row's 512-byte code (linear in rows, 32 times less data than the float32 scan) and then reads exactly the rescoring depth in full vectors; the exact engines read every vector. The rescoring depth needed for a given recall grows with the store: recall@50 of 0.99 took a depth of 400 at 10,000 rows, 800 at 100,000 and 1,600 at 1,000,000, roughly doubling per tenfold growth on this data, at almost no cost in time because the code scan dominates. At equal recall the graph was not faster than the compact scan at 10,000 or 100,000 rows: at 100,000 rows HNSW needed 15.5 ms to reach 0.997, where the compact scan reached 0.995 in 5.1 ms and 0.999 in 5.7 ms. The graph also cost 107 s to build per 100,000 rows on six threads and as much memory again as the vectors, and 4 of 2,000 sampled rows could not be found by their own vector at ef 100. At 1,000,000 rows the graph has no result: its build, with the float32 arena beside it, needed about 34 GB, pushed the shared 64 GB machine into memory compression and was stopped unfinished after 23 minutes. That is itself a statement of what a graph costs at this size: a second copy of the vectors and a build measured in tens of minutes on every load, or else an index file to keep.

### End to end through Palace, one thread

| Path | 10,000 rows, p50 (p95) | 100,000 rows, p50 (p95) | recall@50 at 100,000 |
|---|---|---|---|
| Released | 28.6 (38.5) | 233 (242) | 1.000 |
| Accelerated, exact engine | 8.1 (17.6) | 21.7 (32.3) | 1.000, rank-identical to the released path in every query compared |
| Accelerated, compact scan, rescore 800 | 8.3 (17.9) | 11.2 (21.4) | 0.995 |

Metadata filters stayed exact in every case: zero rows outside the filter across selectivities of 50 %, 10 %, 1 % and 0.1 % at both sizes, with recall@50 of 1.000 for the exact engine and 0.997 to 1.000 for the compact scan. A selective filter made the accelerated search cheaper, where the released scan did not benefit: at 0.1 % selectivity and 100,000 rows the compact scan took 7.9 ms end to end against 268 ms.

### Many concurrent queries (the primary gate)

Throughput in searches per second, one process, N threads, 6-second rounds, three rounds each. One 100,000-row index:

| Threads | Released | Released + memory map | Accelerated, exact | Accelerated, compact | Compact vs released, medians | Worst compact round vs best released round |
|---|---|---|---|---|---|---|
| 1 | 4.1 (3.6–4.2) | 7.1 (5.4–7.2) | 43 (23–47) | 82 (69–89) | 20× | 16× |
| 2 | 3.4 (3.1–3.5) | 12.0 (9.3–12.0) | 86 (65–87) | 214 (148–215) | 63× | 42× |
| 4 | 4.9 (3.8–5.0) | 18.1 (15.0–18.3) | 115 (95–116) | 265 (197–272) | 54× | 39× |
| 8 | 2.6 (2.6–2.7) | 19.6 (17.5–20.1) | 125 (96–125) | 187 (146–189) | 72× | 54× |
| 16 | 2.4 (2.4–2.4) | 12.9 (12.3–13.2) | 118 (110–122) | 155 (130–156) | 65× | 54× |

Median latency at 8 threads: released 3,090 ms (p99 3,115), memory-mapped 407 ms (p99 532), exact 62 ms (p99 114), compact 43 ms (p99 69).

Six 100,000-row indexes, queries spread uniformly:

| Threads | Released | Released + memory map | Accelerated, exact | Accelerated, compact | Compact vs released, medians | Worst compact round vs best released round |
|---|---|---|---|---|---|---|
| 1 | 4.0 (3.6–4.6) | 5.6 (5.1–7.1) | 40 (32–49) | 65 (47–108) | 16× | 10× |
| 4 | 5.8 (5.5–6.7) | 16.5 (16.1–19.8) | 77 (69–106) | 162 (130–252) | 28× | 20× |
| 8 | 3.5 (3.4–3.8) | 22.3 (22.0–27.2) | 87 (85–115) | 134 (131–181) | 38× | 34× |
| 16 | 3.2 (3.1–3.2) | 26.4 (25.2–29.2) | 106 (102–114) | 122 (107–161) | 38× | 33× |

With the same six indexes under a Zipf-skewed load (s = 1.2) at 8 threads: released 3.4 (3.2–3.4), memory-mapped 20.7 (19.2–24.3), exact 97 (93–124), compact 153 (114–184).

One 1,000,000-row index, 20-second rounds, two rounds, median search time in milliseconds:

| Threads | Released | Released + memory map | Accelerated, exact | Accelerated, compact |
|---|---|---|---|---|
| 1 | 2,050–2,063 | 1,919–1,965 | 155–157 | 52 |
| 8 | 29,852–30,042 | 26,620–26,681 | 566–567 | 142–144 |

The compact column is at 800 candidates, which at this size is recall@50 of 0.974. At 1,600 candidates (0.991), in one later round, it took 69 ms at one thread and 212 ms at eight, 14 and 37 searches per second. At this size the released path answers eight concurrent queries in about 30 seconds each (0.3 per second in total); the compact scan answers 37 to 56 per second depending on depth and the exact engine 14. The memory-map setting no longer helps, because the file is far larger than this SQLite build will map.

One query spanning several stores (Palace's multi-store search), median latency in milliseconds at one thread, then at eight concurrent queries:

| Stores in the query | Released, 1 / 8 threads | Accelerated exact, 1 / 8 | Accelerated compact, 1 / 8 |
|---|---|---|---|
| 2 | 450 / 4,432 | 61 / 160 | 21 / 107 |
| 4 | 889 / 9,740 | 111 / 325 | 34 / 169 |
| 6 | 1,295 / 16,913 | 167 / 481 | 47 / 221 |

With the memory-map setting the released path took 257–297 / 571–817 ms for two stores, 514–629 / 1,279–1,561 ms for four and 766–900 / 2,146–2,417 ms for six (two rounds; this mode was rerun after a harness fault, as the spike's README records).

Two things in these curves are limits, not wins. The accelerated paths do not scale linearly: compact-scan throughput peaked at four threads and fell by about 30 % at eight and 40 % at sixteen, which is the interpreter lock in a pure-numpy prototype. Coarser blocks (65,536 rows instead of 8,192) raised the four-thread peak from 265 to about 318 searches per second and changed nothing at eight or sixteen threads, so the limit is not the number of scan calls. And the released path's own collapse is what makes the ratios so large at high concurrency; against the memory-mapped setting the compact scan's advantage is about 10× at eight threads on one index and 6× across six.

Under open-loop Poisson arrivals over the six indexes with eight workers and a 64-deep queue, the compact scan held queue delay to a p95 of 2–20 ms at half its closed-loop capacity and 16–204 ms at 80 % (two rounds); at 95 % and 120 % the bounded queue filled and refused the excess (183–531 of roughly 1,000–1,300 arrivals) with queue delay capped near half a second. Backpressure is by refusal at the queue, never by unbounded waiting.

### Reads while indexes are updated

Six indexes, eight reader threads, and one writer per store committing inserts, deletes and paired delete-and-insert edits through Palace's writer lock. Every change became a probe: a query for the changed row's own vector, judged against the commit it followed.

| Offered changes per second | Released reads/s | Memory-mapped reads/s | Accelerated exact reads/s | Accelerated compact reads/s | Compact p50 / p95 / p99, ms |
|---|---|---|---|---|---|
| 0 | 3.9–4.4 | 17–19 | 122 | 179–204 | 39–45 / 50–51 / 55–56 |
| 2 | 3.2–3.8 | 18–20 | 122–123 | 180–187 | 42–44 / 51–52 / 58 |
| 20 | 2.9–3.6 | 18–19 | 114–115 | 168–173 | 44–46 / 61–64 / 67–68 |
| 200 | 1.7 | 18 | 55–56 | 72–74 | 108–111 / 124–125 / 132–133 |
| Bursts of 100 per transaction (270–500 per second achieved) | 1.3 | 19–20 | 102–103 | 112–118 | 64–67 / 97–111 / 142–157 |

- **Correctness.** 4,573 probe queries ran against the accelerated paths across these runs. None returned a result that was stale with respect to a commit that had returned before the query started, and none was incoherent (both or neither side of an edit). The same oracle over the released path (421 probes) also found none, which checks the oracle.
- **In-flight behaviour.** A commit injected between admission and retrieval, for one store and for the second store of a two-store query, gave the same outcome on the released and accelerated paths: the in-flight query answered from its pinned snapshot, identical to the result before the commit, and the next query saw the commit.
- **Freshness cost.** Visibility lag is zero by contract. The work of applying changes landed on the queries that needed it: at 200 changes per second it took about 2.0 s of the run's 64 reader-thread-seconds, roughly 0.9 ms per applied change in this harness against 0.1 ms when changes are handed over in process.
- **What slowed.** At 200 changes per second accelerated read throughput fell by about 60 % and the median rose from about 40 ms to about 110 ms. The refresh work above explains part of it. The rest is unattributed: the harness's six writer threads share the reader process and its interpreter lock, and the index-level test, where changes arrive without SQLite, showed no reader slowdown at the same rate. Accelerated reads were still more than 40 times the released path at that write rate.
- **Memory.** No update duplicated an index. In a fresh process holding the six indexes (10.49 GB after loading), ten seconds of 200 changes per second (2,017 changes) left the footprint at 11.02 GB, which was also its peak. The indexes themselves grew by 1,606 rows (about 27 MB) and replaced 1,226 stamp arrays of 64 KiB; most of the remaining half gigabyte is the harness's own retained probe queries. There was no moment at which a second copy of any index existed.

### Several warm indexes under memory pressure

Six indexes of 1.81 GB each (as allocated), eight threads, Zipf-skewed unless noted, 15 seconds:

| Budget | Indexes that fit | Searches per second | p50 / p95, ms | Loads forced | Thread-seconds spent waiting on a load |
|---|---|---|---|---|---|
| Unlimited | 6 | 120 | 67 / 97 | 0 | 0 |
| 5.5 GB | 3 | 19 | 36 / 1,976 | 23 | 73 |
| 5.5 GB, uniform load | 3 | 12 | 61 / 2,047 | 25 | 78 |
| 2.0 GB | 1 | 8.5 | 991 / 2,018 | 33 | 70 |

A budget smaller than the set of stores actually in use makes the cache thrash: every miss is a reload of about 0.7 s per 100,000 rows, and queries queue behind it. The accounting bound held (never more than the budget's worth of indexes resident), but the process footprint did not follow it down: it peaked at 9.5 GB under the 5.5 GB budget, because memory freed by an eviction was not returned to the OS in the measurement window. An implementation has to reuse evicted blocks rather than free and reallocate them for the budget to be a real bound.

### Restart, reopen and recovery

There is nothing to recover: the accelerator is rebuilt from the store. A fresh process loaded 100,000 rows in 0.53 s with the OS cache warm and 0.70 s on the cold proxy, and answered its first query in 8–9 ms and the next in 5.2 ms; the released path's first query in a fresh process took 228 ms warm and 350 ms on the cold proxy. A million rows loaded in 5.4 s warm and 7.3 s on the cold proxy, with a first query of 49–73 ms; the released path's first query there took 2.0 s warm and 3.3 s on the cold proxy. A writer killed inside an open transaction of 200 inserts left no feed rows, the database passed its integrity check, the load saw exactly the committed rows, and a later committed change was applied normally.

### Authorization after warm-up

With the store's accelerator resident and warm, the authorization hook was called once per query (25 calls for 25 queries, all cache hits). After the grant was withdrawn, the next 25 queries were all refused, returned no rows, and the store's accelerator was evicted on the first refusal. After the grant was restored the first query reloaded it (80 ms at 10,000 rows, 715 ms at 100,000).

## Costs

### Memory

The accelerator holds one copy of the store's vectors. At 4096 dimensions a row is 16,384 bytes of float32, 512 bytes of code and about 90 bytes of norm, stamps and id. Measured process footprint was 1.78 GB per 100,000 rows and 10.6 GB for the six-index fleet. The 1,000,000-row index measured 17.2 GB. It is linear in dimension, so a store at a smaller identity dimension costs proportionally less (not measured).

- **It can be halved.** In a standalone measurement at 100,000 rows, keeping the rescoring copy as float16 gave the same recall@50 as float32 at every rescoring depth (0.9949 at 800) for 8,192 bytes per row; an int8 copy gave 0.990 for about 4,100 bytes per row. Returned distances then carry that precision.
- **It cannot be mostly avoided.** Keeping only the 1-bit codes in memory (about 0.6 KB per row) and rescoring from the store is not viable at v0.6.0, because reading one vector back costs about 1.7 ms.
- **A graph would add to it, not replace it.** hnswlib keeps its own copy of the vectors: 16.6 KB per row measured, on top of the arena unless the arena is dropped for sealed rows.
- **It is per process.** Two processes that both accelerate a store hold two copies, unless they share a memory-mapped one, which was measured for speed (parity) but not for sharing.
- **Updates add what they add.** An inserted row costs one row. A deleted row costs nothing new and frees nothing until compaction. Nothing is ever copied wholesale.

### Loading and restart

One pass over the store: 130,000–210,000 rows per second by reading sqlite-vec's storage tables (0.5–0.7 s per 100,000 rows), or 600–1,200 rows per second through its SQL interface (8–16 s per 10,000 rows over three runs). The fast path is tied to the sqlite-vec version it was written for and refuses any other. There is no index file to keep consistent, back up, encrypt or invalidate.

### Disk

The change feed costs one extra copy of each inserted vector until pruned (2,000 inserts grew the database by 68.6 MB with the feed against 34.8 MB without). Writer time per row did not measurably change.

### Administration

What an operator or maintainer takes on by enabling this, most consequential first:

1. **A memory budget per host process, sized to the stores actually in use.** Under-sizing does not fail; it thrashes, with second-long tails.
2. **A long-lived process.** A one-shot command gains nothing from a process-local cache and should use the memory-map setting instead.
3. **The change feed's discipline.** Every vector write site appends to it; it is pruned; deletes clear it; a full rebuild resets it. A lapse is a silently stale index, so it needs a tripwire and tests.
4. **Re-qualifying the loader when sqlite-vec changes.** The self-check refuses an unknown layout, which turns an upgrade into a visible fallback or refusal, not a wrong answer.
5. **One explicit failure-behaviour choice** per deployment.
6. **A recall knob for the compact scan** (the rescoring depth), which is only meaningful after the representative evaluation.
7. **A larger concurrency test surface** in the gates: visibility, coherence, eviction and authorization under load.

Not on this list: a new runtime dependency (the recommended engines need only numpy, which Palace already installs), a daemon, an index file, a build step, or a background rebuild.

## Guidance by workload

- **Small stores: leave it off.** The released search costs about 5 ms of fixed work plus about 2 ms per thousand rows at this width. At 10,000 rows the whole gain is from 29 ms to 8 ms; below a few thousand rows there is nothing worth the memory and the administration.
- **One-shot commands: memory map only.** It is the only improvement a short-lived process can use, and it needs nothing.
- **A long-lived host with stores of tens of thousands of rows or more, or with concurrent queries: accelerate.** This is where the gain is large and repeatable: 20× for a single query stream at 100,000 rows and 38–72× under eight concurrent ones, and about 6–10× over the memory-mapped setting.
- **Exact engine or compact scan.** They cost the same end to end at 10,000 rows, where fixed work dominates, so use the exact engine there and whenever results must be identical to the released path. At 100,000 rows the compact scan is about twice as fast (11 ms against 22 ms, and 187 against 125 searches per second at eight threads) for a recall@50 of 0.995 on this data; it is the choice once the evaluation supports it. At 1,000,000 rows the gap is wider, 50 to 70 ms against 155 ms, with the rescoring depth raised to 1,600 to hold the recall target.
- **Stores under constant ingestion: nothing special to do.** The append-only structure was the only strategy that kept readers, freshness and memory intact together. Never update a serving graph in place.
- **Tight memory: budget for the hot set or do not enable it.** If the stores in use do not fit, the memory-mapped released path is the better answer for the ones that do not; a float16 rescoring copy doubles what fits.
- **Graph indexes: not at these sizes.** At 10,000 and 100,000 rows a graph was slower than the compact scan at equal recall, and at 1,000,000 rows it was not measured. The compact scan grows linearly, about 47 ms per million rows on one thread, and a graph does not, so a crossover exists somewhere beyond the sizes measured. Where it lies is unknown, and a graph would bring a second copy of the vectors, a long build on every load and a tail to seal.

## Not measured

Stated plainly, so that none of it is mistaken for a result:

- **Platform.** Everything ran on macOS arm64 on one machine. Linux, other CPUs, and any confined or encrypted-volume environment were not measured.
- **Real data and recall.** Only seeded synthetic unit-norm vectors at 4096 dimensions. No real corpus, no remote embedding model's vectors, no other dimension, and not the representative evaluation Palace requires before a retrieval change. The generator was earlier calibrated against a small real set and found to have denser neighbourhoods, which makes these recall figures conservative, but that is not a substitute.
- **Search modes.** Vector mode only. Hybrid and reranked searches add the keyword leg and the reranker, which this work does not touch; they dilute the relative gain and were not timed.
- **Cold start.** "Cold" here is a proxy: a fresh process on a copy written and read with the OS cache bypassed. Eviction of the cache was not proven and the machine was not rebooted. Power loss was not tested.
- **Parts of the design that were not built.** Compaction of deleted rows; immediate excision other than evicting the store's accelerator; pruning of the change feed and the reload it forces; the rule that a delete clears earlier feed vectors, and `secure_delete` on the feed; a background refresher; a feed carrying ids only, under load.
- **Scale-out.** One process only. Several processes sharing one memory-mapped copy, and a free-threaded interpreter, were not tried. The float16 and int8 rescoring copies were measured standalone at 100,000 rows, not in the load harness.
- **Graph engines.** hnswlib only, with one build setting (M = 16, ef_construction = 200), and no graph result at 1,000,000 rows because the build was stopped. usearch was installed and inspected, not benchmarked; quantized graphs were not tested.
- **Memory under real pressure.** Freed memory was not returned to the OS within the measurement window on this allocator, so the figures say what peaks were reached, not that memory comes back after eviction. Swap was never engaged.
- **Larger and stranger loads.** No store above one million rows, no more than six stores loaded at once, and offered write rates up to 600 changes per second. The harness's writers were threads in the reader process using their own connections and Palace's writer lock; a writer in another process was exercised only in the recovery test.

## Review needs

Before any implementation is approved, these want an independent read:

1. **The visibility contract and its test design.** The stamp rule, the refresh-to-own-snapshot step and the multi-store behaviour are the correctness core. A reviewer should try to construct a schedule that returns a row deleted before the query's snapshot, or drops a row that snapshot contains.
2. **The change feed.** Every site that writes or deletes a vector row must append to it in the same transaction (v0.6.0 has four statements: three in `palace/index/core.py` and one in `palace/index/store_ops.py`), and a full rebuild must reset it. A missed site is a silently stale accelerator. The review should decide the tripwire (for example a row-count check at load and at intervals) and the pruning bound.
3. **Deletion residue.** The rule that a delete clears earlier feed vectors for the same chunk, the `secure_delete` requirement for stores that need it, and whether excision should evict the store's accelerator or wait for compaction.
4. **The loader's dependence on sqlite-vec's storage layout.** It reads the library's internal tables for speed, guarded by a version check and a byte-for-byte self-check against the SQL interface. The alternative is the SQL path at 600–1,200 rows per second, which makes loading a million rows take a quarter to half an hour. Whether to accept the coupling, ask upstream for a supported bulk read, or store a second copy of vectors in a Palace-owned table is a real choice.
5. **The failure-behaviour setting** described above, which is the operator's to choose.
6. **Recall evidence for the compact scan.** Palace requires a representative personal evaluation before a change that can alter retrieval results; the synthetic vectors here do not satisfy that. The exact in-RAM engine returned rank-identical results to the released path in every query compared and does not change retrieval, so it can precede that evaluation; the compact scan cannot.
7. **Interpreter-lock scaling.** The prototype is pure numpy, and its throughput in one process peaked at four threads. A reviewer with profiling time should say whether a coarser block size, fewer Python-level steps, or a small native kernel is the right remedy, and whether the target is one process or several sharing a memory-mapped copy.

## Implementation and release plan

Conditional on the gate this brief stops at. Nothing below is started.

1. **Memory-mapped reads on the released path.** One setting on the read-only connection, measured here as a 1.7× to 7.5× gain with no index, no memory budget and no change in results. It is independent of everything else and should be decided on its own. It needs a statement of its limits (the SQLite build's mapping cap; behaviour on I/O error) and a Linux measurement, which this spike did not make.
2. **Change feed.** The table, the writer appends at every vector write site, the delete-clears-vector rule, pruning, and the reset on full rebuild. Additive; a store without the table is simply not accelerated. Hermetic tests: every write path appends; a killed writer leaves nothing; pruning past a reader forces a reload.
3. **Accelerator core with the exact in-RAM engine only.** Blocks, stamps, views, refresh, the cache with budget, single-flight loads and the authorization hook, the explicit accelerators argument on the search entry points, and the configuration with exact as the default. Because results are identical to the released path, this phase changes no retrieval behaviour. Acceptance: the released path's tests pass unchanged with acceleration off and import no new module; rank-identical results with it on; the visibility tests of this spike rebuilt as hermetic tests (a commit injected between admission and retrieval, single-store and multi-store; deletes, inserts and paired edits under concurrent readers); the memory budget holds under skew.
4. **Compact scan engine.** Gated on the representative evaluation. Includes the choice of rescoring depth and, if memory matters to the consumer, a float16 rescoring copy, which halved the memory at equal recall in a standalone measurement here.
5. **Compaction and explicit excision.** Block-at-a-time reclamation and a call that removes one store's vectors from memory immediately.
6. **Release.** Each phase is releasable on its own: acceleration stays off by default, the configuration and the new table are additive, and a consumer that never constructs a cache runs the released code. The release notes must state the memory cost per row, the long-lived-process requirement and the sqlite-vec version the loader is qualified against.

Graph indexes are not in this plan. The segment mechanism leaves room for one later if a single store grows well past the sizes measured here.

## Reproduce

The spike lives outside this repository, in an isolated directory beside the checkout (`palace-vector-cache-spike/`), and writes to no Palace checkout. Its `README.md` gives every command; its `FIXTURES.md` catalogs the vectors and software with hashes; its `MANIFEST.sha256` covers scripts, raw results, logs and ground truth. The released Palace source it runs is `git archive v0.6.0` installed with that tag's locked dependency versions. The prototype routes Palace's one vector-leg function (`palace.search._vector_search`) through the accelerator for the current process and restores it afterwards; no Palace file is modified, and the baseline rows are that function untouched.

In outline: build stores with Palace's own `init_db` from saved vectors (no embedding model is called, so embedding time is excluded from every number by construction); compute exact float64 ground truth; then

```sh
sh scripts/run_main.sh
```

```sh
sh scripts/run_extra.sh
```

```sh
sh scripts/run_1m.sh
```

```sh
env/venv/bin/python -I scripts/summarize.py
```

and the standalone probes named in the README (update strategies, visibility, SQLite settings, change-feed cost, rescoring precision, memory-mapped sidecar). Each raw result is one JSON line carrying its own UTC timestamp; the tables in the generated summary give the time span of each group.

## Sources

- hnswlib 0.8.0 README, `https://raw.githubusercontent.com/nmslib/hnswlib/v0.8.0/README.md`, retrieved 2026-11-19; not redistributed here. Quoted: `add_items` is "Thread-safe with other `add_items` calls, but not with `knn_query`"; `knn_query` is "Thread-safe with other `knn_query` calls, but not with `add_items`"; `mark_deleted` "marks the element as deleted, so it will be omitted from search results"; the Python filter "works slow in python in multithreaded mode".
- Faiss wiki, "Threads and asynchronous calls", `https://raw.githubusercontent.com/wiki/facebookresearch/faiss/Threads-and-asynchronous-calls.md`, retrieved 2026-11-19; not redistributed here. Quoted: "Faiss CPU indices are thread-safe for concurrent searches, and other operations that do not change the index"; "A multithreaded use of functions that change the index needs to implement mutual exclusion."
- USearch 2.26.6 package description (the README shipped in the installed distribution's metadata), read 2026-11-19. It states that the index "is concurrent by design" and lists filter predicates as unavailable in the Python binding; it gives no explicit guarantee for overlapping add and search at this version, so none is assumed.
- Palace v0.6.0 source at the tag commit above; sqlite-vec 0.1.9 official wheel (`vec0.dylib` SHA-256 `193e480c50b59a55977d166f4aaf0e1bc8832d6963516e5950f39e4d2ce0b793`).
