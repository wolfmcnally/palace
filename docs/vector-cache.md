# Opt-in exact in-memory vector cache

A long-lived host process that runs many searches can serve the vector leg of each opted-in store from a process-local copy of its vectors instead of scanning `chunks_vec` with sqlite-vec on every query. The answer is the released answer: the same ids, order and distances, filters and the legacy filtered missing-vector behaviour included. A store or process that does not opt in runs exactly the released code; the exhaustive SQLite search stays the default and needs no build, warm-up or extra dependency.

Two opt-ins are needed, one per store and one per process.

## 1. Enable the store's change feed

```sh
palace index vector-feed enable --store /path/to/store
```

This creates two tables (`chunks_vec_feed`, `chunks_vec_feed_state`) and two triggers on `chunks`, under the store's writer lock, and starts a lineage (a random epoch). From then on every Palace writer (`palace index build` and `update`, the index daemon, `palace index copy` and `remove`) records each committed vector insert and delete, with its bytes, in the same transaction. Writers keep at most `VECTOR_FEED_TAIL_ROWS` (8,192) entries and prune the oldest by sequence range; a cache that falls behind the pruned floor reloads. A certified full rebuild (`palace index build --full`) starts a new lineage.

**Deleted vectors.** A delete clears the payloads of earlier feed inserts of the same chunk in the same transaction, so no later SQL reader can retrieve a removed vector from the feed. Writers of a feed-enabled store also turn on SQLite's `secure_delete` for their connection, so the pages that delete frees are overwritten with zeros in the database file. Two limits remain. The write-ahead log holds earlier page images until it is checkpointed, and a reader whose snapshot began before the delete can still read the row until its transaction ends. A process's vector cache holds the removed vector in memory until its block is compacted or the store is evicted; `cache.evict(store)` is the immediate remedy and costs one reload. Physical erasure is verified only for the database file after a checkpoint; storage below SQLite (file-system snapshots, backups, the storage device) is outside this guarantee. A store that never enables the feed keeps SQLite's default behaviour.

```sh
palace index vector-feed show --store /path/to/store --json
```

prints, in constant work and without opening the store for writing:

| Field | Meaning |
|---|---|
| `enabled` | whether the feed exists |
| `epoch` | the current lineage |
| `last_seq`, `floor_seq` | the newest feed position and the pruned floor |
| `live_chunks` | stored chunks, from monotonic insert and delete totals |
| `vector_rows` | stored vectors, maintained by every feed-aware write |
| `chunks_without_vector` | `live_chunks − vector_rows` |
| `feed_entries` | entries held for replay |
| `diverged` | a writer changed `chunks` without the feed |

`palace index vector-feed disable` drops every feed object. A store that never enabled the feed has no feed object and no writer adds one; its writers' statements are unchanged apart from one schema-catalog read per transaction. The feed module is imported only by a process that writes, and a search never imports it or the cache.

**Divergence.** The triggers count every insert into and delete from `chunks` by any writer of any version; feed-aware writers count the same rows they write. Unequal totals mean a write bypassed the feed (an older Palace, or hand-edited SQL). A cache refuses such a store as an integrity failure, never a fallback. Re-running `enable` starts a new lineage from the store as it is. The detectable class is any change that inserts or deletes a `chunks` row, including a same-count replace. A raw rewrite of `chunks_vec` alone, without touching `chunks`, is outside the supported writer contract and is not detectable.

## 2. Pass a cache to prepared execution

```python
from palace.vector_cache import VectorCache
from palace.search import execute_prepared_search
from palace.multistore import execute_prepared

cache = VectorCache(
    budget_bytes=8 * 2**30,
    on_unavailable="fallback",       # or "refuse"; there is no default
    authorize=lambda store: allowed(store),  # optional, runs on every execution
)
result = execute_prepared_search(prepared, vector_cache=cache)
for outcome in result.vector_cache:
    ...
```

One `VectorCache` is meant to live for the whole process and be shared by every thread. `execute_prepared_search` and `execute_prepared` take it as the keyword `vector_cache`; nothing else changes. `cache.warm(store)` authorizes, admits and loads a store ahead of its first query. `cache.evict(store)` unpublishes it at once; its memory returns when its last lease ends. The cache identifies every store by its resolved directory, so a relative path, a symbolic link and the real path name one resident copy, and the authorization hook always receives the resolved directory.

### Per execution

1. The authorization hook runs for every selected store, before any store is admitted, on every execution including hits. A false result or an exception fails the execution and evicts that store; with several stores, a denial of any one stops the execution before any retrieval.
2. The cache then takes one lease that pins each store's resident view. A store not yet resident is loaded once, with concurrent first executions sharing the load. An eviction or a revoking denial while a load is outstanding fences that load and every execution waiting on it: nothing is published or pinned, and those executions report `snapshot`.
3. Admission runs as released. The first vector leg of each store reads its snapshot's feed position, applies the feed up to it and scans only rows visible at that position. Every leg, the rerank and evidence of the execution use the same pinned view, and the single-store path keeps its SQLite snapshot open through the rerank when a cache is passed, as the multi-store path always does; a commit after admission is invisible to the execution and visible to the next one.

### Results

`RerankedSearch.vector_cache` and `MultiStoreSearch.vector_cache` hold one `VectorCacheOutcome` per store, in order, when a cache was passed (otherwise `None`):

- `cached_legs`: legs answered from memory.
- `oracle_legs`: legs answered by the released SQL on the same connection because parity could not be certified. Equal distances are ordered by chunk id in both the cached and the released search, so ties of any size, including a tie at the cut, are answered from memory; when a tie at the cut reaches sqlite-vec's 4,096-row KNN limit, the released search ranks every row by the same scalar distance to find its members. Reasons: `missing-vector` (the filter admits a chunk without a vector, where the released search returns no rows), `relocated` (a filtered row moved by a concurrent compaction), `knn-limit` (a pool above sqlite-vec's 4,096, where the released search refuses), `non-finite-query`, and `numeric-range` (a nonzero component of the query or of a visible stored vector below 2⁻¹²⁶ or above 2⁴⁰, outside the range the error bound is proved for).
- `fallback_legs` with `reasons`: legs answered by the released SQL because the cache was unavailable under `on_unavailable="fallback"`.
- `bypassed`: the store had no sqlite-vec vector leg (keyword-only search, an S3 Vectors store, or no database).
- `snapshot_seq`: the feed position the legs read.

### Unavailable versus failed

Only these operational conditions honour `on_unavailable`: `feed-disabled` (the store has no feed), `budget` (a reservation was refused under `budget_bytes`), `queue-full` (more than `max_waiting_loads` executions waiting for loads) and `snapshot` (the execution's snapshot is older than anything the pinned view can represent, its lineage changed, the feed was pruned past the view, or the store was evicted while loading). With `"refuse"` they raise `VectorCacheUnavailable` (an `IndexError` with `.reason`); with `"fallback"` the leg runs the released SQL and the outcome says so, but only after the feed state on the connection that answers has been checked, so a fallback never answers over a diverged or malformed feed. Every other failure propagates unchanged, fails the execution under either setting and evicts the store: a diverged feed (`VectorFeedDiverged`), malformed feed state, a vector of the wrong dimension, a load or refresh whose count does not reconcile with `vector_rows` (checked also on a hit at an already-applied position), an authorization refusal, and any SQL error reading the store.

## How exactness is kept

Candidates are ranked by an approximate float32 squared distance with a stated error bound, `γ_{d+2}·(‖x‖ + ‖q‖)²/(1 − γ_d) + 8·d·2⁻¹²⁶` with `γ_n = n·u/(1 − n·u)`, `u = 2⁻²⁴`; the absolute term covers products and squares that underflow, whether gradually or flushed to zero. The bound is proved only while every nonzero component lies in [2⁻¹²⁶, 2⁴⁰], so inputs are normal and nothing overflows; outside that range the leg goes to the oracle. The best `pool + margin` are rescored with sqlite-vec's own `vec_distance_l2` on the query's connection, the function and float32 arithmetic the released KNN uses. The cut is accepted only when the `pool`-th rescored distance squared is below every unrescored row's lower bound, which also allows for sqlite-vec's own float32 summation error (`(1 − γ_{d+4})` relative and `4·d·2⁻¹²⁶` absolute); otherwise the margin doubles (counter `cut.widened`), up to every visible row. Returned distances are sqlite-vec's. Filters resolve to the same chunk set the released metadata constraint uses, and the released search applies them before its KNN cut, as the cache does.

The fast loader reads sqlite-vec's storage tables directly only when `vec_version()` and the table's recorded version both equal `QUALIFIED_SQLITE_VEC_VERSION` (`v0.1.9`). It must produce exactly `vector_rows` unique ids, and sampled rows must equal what the SQL interface returns; otherwise it is discarded and the supported `SELECT chunk_id, embedding FROM chunks_vec` loads the store (counter `loader.fast-rejected`, or `loader.unqualified-version`). If that route does not reconcile either, the load is an integrity failure.

## Memory

Vectors live in blocks of `VECTOR_CACHE_BLOCK_ROWS` (2,048) rows: 32 MiB of float32 data each at 4,096 dimensions (2,048 × 4,096 × 4 bytes), plus per-row stamps and an estimated id overhead. Updates fill free slots in any block, then take new blocks, and stamp deletes; no update copies a block. Compaction rewrites one block, copying its survivors in bounded slices, when its dead rows reach `VECTOR_CACHE_COMPACT_DEAD_FRACTION` (0.25), keeping every row a live lease could still see; while a replaced block is still read by an older view, no further compaction of that store runs, so at most one replaced block per store is outstanding. Under reservation pressure, a refresh that is the store's only lease, with no other view, reuses the slots of rows nobody can see in place (counter `reclaimed_slots`); otherwise the leg is `budget`. An evicted store's blocks are kept until its last lease ends. Freed blocks are zero-filled and pooled for reuse by stores of the same width.

One ledger accounts every block (live, retired while a lease still reads it, or pooled), each in-flight load and the working memory of every query, refresh, load and compaction, reserved before it is allocated. A reservation beyond `budget_bytes` first releases pooled blocks of other widths, then evicts the least recently used store with no lease, and is otherwise refused as `budget`. A small store occupies at least one whole block.

`cache.stats()` reports, from one ledger snapshot and in constant work per store: `budget_bytes`, `owned_bytes` (= live + retired + pooled + load + scratch), `headroom_bytes`, the leases held, cumulative counters (`loads`, `reloads.<reason>`, `evictions.<reason>`, `oracle.<reason>`, `fallbacks.<reason>`, `refusals.<reason>`, `reservation_refusals`, `discarded_loads`, `compactions`, `reclaimed_slots`, `cut.widened`, `loader.*`) and per resident store its epoch, base and applied feed positions, live and dead rows, loader route, load time, owned bytes and leases. The applied position and the row counts are published together after the refresh reconciles, so they always describe a committed state. No figure carries a chunk id, text or query. These byte figures are the cache's own accounting, an estimate; the process's footprint as the operating system sees it differs (untouched block pages are not resident, and the interpreter has its own overhead). `palace.vector_cache.process_peak_rss_bytes()` reports the process's lifetime peak resident set separately.

## Limits

- Exact search only. There is no memory-mapped setting, compact (1-bit) scan or graph index, and the default search path is unchanged, including its filtered missing-vector behaviour.
- One process. Each process holds its own copy; there is no cross-process sharing and no background refresher: an execution applies the feed it needs.
- Numerical work runs in numpy with the interpreter lock released for the large products; heavy concurrent use is bounded by the interpreter, not by a native kernel.
- macOS (ARM64) is exercised locally; Linux is exercised only by hosted CI.

## Qualification bench

```sh
./bin/palace-vector-cache-bench --store "$(mktemp -d)"
```

builds a synthetic 20,000-chunk store in the given empty directory, then times interleaved released and cached executions of the same prepared vector searches, checks that every pair of answers is equal, and prints medians, the 95th percentile, the speedup, leg routes, owned bytes and peak resident memory as JSON. It is evidence for an operator's decision, not a gate.
