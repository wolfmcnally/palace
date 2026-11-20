# Get Started

## Provision a checkout

Palace uses Python 3.12 through the repository's managed runtime and committed uv lockfile. Install `uv`, Git, Bash, curl, jq and OpenSSL on the host, then provision the environment from the repository root:

```bash
./bin/setup
```

Run the complete checks:

```bash
./bin/check all
```

The optional editable tool installation puts `palace` on your PATH. It has its own dependency environment, so reinstall it after a runtime dependency change:

```bash
uv tool install --editable .
```

## See a first result without a model service

The synthetic multi-store walkthrough indexes and searches fixture stores with local stubs: no downloads, credentials or paid services. After `./bin/setup`, start it with:

```bash
./bin/palace-multistore-demo setup
```

Then follow the walkthrough in the repository's `docs/multi-store.md` for the search and cleanup commands.

## Build and search a real store

Install and start [Ollama](https://ollama.com/), then acquire the default embedding model (approximately 5 GB):

```bash
ollama pull qwen3-embedding:8b
```

Index a small Markdown source directory into a separate, disposable store, replacing the example paths:

```bash
palace index build --store /path/to/store --watch-root /path/to/source
```

Search it:

```bash
palace search --store /path/to/store "retention policy"
```

Default search may download the pinned local reranker on first use; `--no-rerank` avoids that download. Each index records its embedding model, provider, dimension and convention. Switching that identity requires a full rebuild, and a query with a mismatched identity is refused rather than answered with substituted vectors.
