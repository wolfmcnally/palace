# Palace

Palace is a local-first retrieval-augmented generation (RAG) system for AI agents and applications, created by Wolf McNally. It makes documents and structured events available as relevant, traceable context for language models, combining keyword and semantic search with optional query expansion and reranking.

Applications can search a single knowledge store or combine results across multiple stores while preserving their origins. Authoritative content stays in inspectable files, and search indexes can be rebuilt. Local embedding and reranking models are the defaults, with explicitly configured alternatives.

Palace provides library and command-line interfaces for building agent memory, personal knowledge tools, and other RAG applications, with explicit control over storage, provenance, and inference boundaries.

Version 0.1.0 is the first tagged MIT release. Package registry publication is separate. APIs and storage contracts remain experimental; review the changelog before upgrading.

## Supported retrieval surfaces

- Synchronous indexing reconciles changed, unchanged and deleted files into an explicitly selected store.
- Hybrid retrieval combines lexical and vector ranks, with optional query expansion and configurable reranking.
- Multi-store retrieval binds each embedding identity, preserves store origin, fuses ranks and reranks the candidate union once.
- Local Ollama embeddings and the local ONNX reranker remain defaults. An explicitly configured published corpus may use the existing third-party embedding path. Private HTTPS embedding/reranking requires a declared operator-controlled boundary, credential references and the native adapter; the personal store and vault remain protected.
- Files, provenance and raw events remain independently inspectable. Capture, watch, reindex and other macOS integration tools are source-checkout features with their own operating procedures.

Synchronous indexing/search and the hermetic provider fixtures are qualified on macOS ARM64 and Linux ARM64. This does not establish Linux daemon support, x86 qualification, a live inference deployment or model quality. The personal retrieval evaluation remains pending; synthetic fixtures prove behavior, not comparative relevance or production latency.

## Development checkout

Use Python 3.12 through the repository's managed runtime and committed uv lockfile. Install `uv`, Git, Bash, curl, jq and OpenSSL on the host, then provision the environment:

```bash
./bin/setup
```

Run the complete checks:

```bash
./bin/check all
```

Inspect the public command surface:

```bash
./bin/python -m palace.cli --help
```

The optional editable tool installation exposes `palace` on PATH:

```bash
uv tool install --editable .
```

It has its own dependency environment. After a runtime dependency change, reinstall it explicitly. Do not install or restart services merely to try synchronous retrieval.

## First result without a model service

From this checkout, the [synthetic multi-store walkthrough](docs/multi-store.md) demonstrates indexing and retrieval without downloads, credentials or paid services. Run `./bin/setup` first, then start with `./bin/palace-multistore-demo setup` and follow the guide.

## Build and search a real store

Install and start [Ollama](https://ollama.com/), then acquire the default embedding model (approximately 5 GB):

```bash
ollama pull qwen3-embedding:8b
```

Keep Ollama running; `OLLAMA_HOST` selects a different service address. After the editable tool installation above, create a small Markdown source directory and index it into a separate disposable store. Replace the example paths below. Default search may download the pinned local reranker on first use; `--no-rerank` avoids that download. See [index configuration and operations](palace/index/README.md) for provider identity and rebuild rules.

```bash
palace index build --store /path/to/store --watch-root /path/to/source
```

```bash
palace search --store /path/to/store "retention policy"
```

Reflect individual files as they change, from any number of concurrent processes, without walking the tree:

```bash
palace index update --store /path/to/store --watch-root /path/to/source /path/to/source/changed.md
```

Every writer of a store serializes on a per-store lock the operating system releases with its holder; embedding runs outside it and only commits wait. Each index records model, provider, dimension and embedding convention. Switching identity requires `palace index build --full`; mismatched queries refuse. There is no silent provider substitution. The local reranker may acquire its pinned model artifacts on first use; these production examples are not an offline fixture.

Repeat `--store` for explicit multi-store search:

```bash
palace search --store /path/to/store-a --store /path/to/store-b --json "retention policy"
```

Library callers use `palace.index.build.build`, `palace.index.update.update`, `palace.search.search`, `palace.search.search_reranked` and `palace.multistore.search_stores`. Reranked results expose `applied`, `disabled` or `failed`; multi-store hits carry their canonical store and local hit. Private and multi-store reranking fail strictly by default; deliberate operational degradation requires explicit opt-in. Authentication, identity, schema and audit errors remain failures.

## Walkthroughs and operations

- [Multi-store walkthrough](docs/multi-store.md): disposable synthetic stores, collision-safe origin, deterministic ordering and guarded cleanup.
- [Private-provider walkthrough](docs/private-providers.md): a local HTTPS fixture, explicit authorization, audited inference and failure examples with no paid provider.
- [Platform qualification](docs/portability.md): supported synchronous paths and deliberate macOS boundaries.
- [Runtime operations](docs/runtimes.md): source-checkout services and their lifecycle.
- [Release checklist](RELEASE.md): exact artifact construction, clean installation and owner publication decisions.

Installed wheels carry the portable Python package, metadata and license. Checkout launchd templates, capture-hook scripts, test harnesses and demonstration launchers are not installed by the wheel; use the full development checkout for those integrations. Source archives are explicitly scoped package-build inputs, not complete development checkouts. No command here deploys a service or changes its authorization boundary.

Exact document tags and other string fields can be imported and queried without inference. See [document metadata](docs/metadata.md) for the library, CLI, portable SQLite contract, and filtered search.

See the [documentation index](docs/README.md), [contributor instructions](CONTRIBUTING.md), and [security reporting instructions](SECURITY.md).

## Agentic development

The checkout retains the methodology, agent skills, policies, portable mirrors and candidate-bound development tooling. See [AGENTS.md](AGENTS.md), the [methodology](briefs/methodology.md), and [policy catalog](policies/README.md). Public planning, lessons and activity records start fresh; private operational records are not part of this release.
