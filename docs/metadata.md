# Document metadata

Palace stores application-defined string sets once per document. Source records remain authoritative; this database is a rebuildable projection. Document discovery works even when a document has no indexed passages. Fields and values use exact, case-sensitive matching; producers own canonical spelling and case. Text occurrences cannot satisfy a metadata predicate.

## Import and lookup

Create a JSON file containing a replacement/deletion batch:

```json
{
  "documents": [
    {"document_id": "doc-17", "fields": {"kind": ["report", "letter"], "visibility": ["public"]}, "watch_root": "/srv/library", "path": "notes.md"},
    {"document_id": "doc-18", "fields": {"kind": ["report"]}}
  ],
  "delete_ids": ["doc-16"]
}
```

```sh
palace metadata replace --store /srv/search --input records.json
palace metadata lookup --store /srv/search --filters '[{"field":"kind","any_of":["report"]}]' --limit 20
palace search budget --store /srv/search --mode bm25 --metadata-filters '[{"field":"kind","any_of":["report"]}]'
```

A replacement supplies the complete field set for that document, removing superseded values. Duplicated values collapse. Unknown deletions and identical replacements are no-ops. Duplicate document operations, including replacing and deleting the same ID, refuse the whole batch. The entire input is validated before writes. Database conflicts roll back the batch atomically. A batch holds the store's writer lock (`<store>/meta/index-writer.lock`) for its transaction, the same lock the indexer's build, daemon and per-file update take, so it neither interleaves with a whole-tree build nor spoils that build's certificate; a holder that keeps the lock beyond the bounded wait (`lock_timeout`, default 300 seconds) is named in the refusal. An owned connection additionally waits up to five seconds for SQLite's own write lock before reporting failure. No metadata operation invokes inference or changes existing chunk text or vectors.

An association requires both an absolute POSIX watch root and a canonical relative POSIX path, exactly as stored by the indexer. Do not resolve symlinks independently or infer an association from a basename. Associations are unique by both strings; paths may be absent from disk. Records without associations still participate in document lookup.

Each filter supplies a field and exactly one nonempty `any_of` or `all_of` array. Any-of means OR within that array, all-of requires every value, and separate filters combine with AND. Malformed predicates refuse; a valid unknown value returns no matches. An empty filter list explicitly requests all documents.

Lookup returns `documents`, `generation`, `cursor`, and `total` (null unless `--count` is requested). Documents include IDs, complete metadata, and optional associations. Results use SQLite BINARY document-ID ordering. Supply the returned cursor for the next page; page limits range from 1 to 1,000. Cursors bind the normalized query and metadata generation, and are rejected after metadata changes or with a different query. They are continuation tokens, not authorization credentials. Counts are optional because they visit all matching IDs. Read and hydration share one database snapshot.

Text and hybrid search apply eligibility before each branch's candidate limit, including query variants and reranker inputs. The CLI currently refuses filtered searches over multiple stores. Missing or unsupported metadata is an explicit unavailable error for filtered retrieval. Unfiltered search remains available independently.

## Library and portable SQLite contract

`palace.metadata` uses only the standard library. Use `metadata_connection(database_path, writable=True)` and `replace_documents(connection, records, delete_ids=...)`, or open a read connection and call `lookup_documents(connection, filters, limit=..., cursor=..., count=False)`. Records are `DocumentMetadata` instances and predicates are `MetadataFilter` instances. Writers require idle connections with `isolation_level=None`; callers retain their configured busy timeout, and the writer derives the store from the connection's database path to take the writer lock (an in-memory database has no co-writers and takes none). Readers can join an existing caller read transaction. SQLite snapshots retain their original generation until that transaction ends.

The component lives beside chunks in `index/chunks.sqlite`. Index bootstrap initializes it independently, including alongside existing indexes. Metadata-only imports also work in text-only SQLite stores without loading a vector extension. Portable consumers may read these tables directly:

| Table | Contract |
| --- | --- |
| `metadata_state` | `key` primary key, `value`; format is `document-metadata-v1`, generation is a random 32-character lowercase hexadecimal token. |
| `metadata_documents` | `document_id` BINARY text primary key; nullable paired `watch_root`, `path`; unique association index. |
| `metadata_values` | `field`, `value`, `document_id`; primary index `(field, value, document_id)` and reverse index `(document_id, field, value)`. |

Consumers must validate the format, read within a transaction, and verify their own source snapshot before publishing or serving a projection. Refreshes must explicitly delete withdrawn document IDs. Metadata generation tracks actual metadata changes; it does not certify a consumer's source freshness or the surrounding text index. No JSON scanning or metadata duplication across passages is required.

## Performance boundary

Selective document lookup starts from matching value-index entries and hydrates only the requested document page. SQL performs union/deduplication and intersection inside SQLite. Work scales with matching entries and requested output rather than the whole corpus. Multiple predicates and broad matches can require substantial intersection, sorting and deduplication work; exact counts necessarily visit every match. Keyset pagination avoids rescanning an offset prefix.

Filtered lexical retrieval also depends on the number of FTS matches; its cost is not just the number of metadata matches. The existing sqlite-vec backend remains exhaustive. Eligibility constrains which candidates it may return, but does not establish sublinear semantic-search cost. Metadata lookup and refresh make no embedding, classification, expansion or reranking calls.

## Store selection and recovery

Commands select the store by `--store`, then `PALACE_STORE`, then the normal personal default. Metadata import can deliberately create an empty store: discovery does not require chunks. Check the target before importing. A wrong root string in a file association produces no passage matches; verify producer associations against the indexer's stored roots.

A partial or unsupported metadata component refuses both metadata access and index bootstrap. Unfiltered reads of an otherwise healthy text index remain available. To rebuild damaged metadata, stop writers, retain the authoritative import file, and remove only this derived component in one SQLite transaction before importing it again:

```sh
sqlite3 /srv/search/index/chunks.sqlite 'BEGIN IMMEDIATE; DROP TABLE IF EXISTS metadata_values; DROP TABLE IF EXISTS metadata_documents; DROP TABLE IF EXISTS metadata_state; COMMIT;'
palace metadata replace --store /srv/search --input records.json
```

Use the actual intended store path. This discards its metadata projection and invalidates all existing metadata cursors; it leaves chunk text and vector tables intact. Reimport the complete authoritative document set before resuming filtered requests.
