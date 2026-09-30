## Relocatable chunk identity

Chunk paths are stored relative to each watch root while the watch_root column carries the absolute root. That representation lets the same chunks.sqlite artifact move between machines without changing chunk identifiers.
