# Embedding venue fixtures

These files are real 4,096-dimensional little-endian `float32` embeddings of
the text recorded in `venues.json`. `ollama.f32` came from local Ollama;
`openrouter.f32` and `openrouter_repeat.f32` are consecutive OpenRouter calls
pinned to the upstream recorded in `venues.json`.

The automated suite only reads these files and never calls either service.
Regeneration is an explicit, network-touching implementation measurement. With
`OPENROUTER_API_KEY` set and local Ollama available, run:

```zsh
probe_store="$(mktemp -d)"
./bin/palace-embed-remote-probe \
  --store "$probe_store" \
  --capture-fixtures tests/fixtures/embed
```

The scratch store is required because every remote HTTP attempt is appended to
its `events/cloud-egress/` audit stream.
