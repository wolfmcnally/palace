# Palace

Language models answer better when they can see the right context, but the documents and events that hold that context are scattered, change often, and must stay under their owner's control. Palace is a local-first retrieval-augmented generation (RAG) system for AI agents and applications: it makes documents and structured events available as relevant, traceable context, combining keyword and semantic search with optional query expansion and reranking.

Authoritative content stays in inspectable files, and every search index can be rebuilt from them. Applications search one knowledge store or combine several while preserving where each result came from. Local embedding and reranking models are the defaults; any alternative provider is explicitly configured, never substituted silently.

Start with [Get Started](getting_started.md) to provision a checkout, see a first result without any model service, and then build and search a real store.

Palace's APIs and storage contracts are experimental; review the changelog in the [source repository](https://github.com/wolfmcnally/palace) before upgrading. The repository's guides under `docs/` cover multi-store retrieval, private providers, the vector cache and operations in more depth.

Palace is developed with the agentic coding methodology of the [Agentic Coding Starter](https://github.com/wolfmcnally/agentic-coding-starter).
