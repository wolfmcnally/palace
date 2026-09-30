## Juniper Observatory caching decision

The fictional Juniper Observatory's sample pipeline keeps its reusable retrieval artifact in SQLite with sqlite-vec. A separate Redis service was rejected because the portable index should ship as one file with the backend.
