# Policy: Deliverable boundary

Palace deliberately uses a flat Python layout: `palace/` is runtime source; `pyproject.toml`, `uv.lock`, `.python-version` and `tests/` live at repository root. Teaching does not relocate this established toolchain into `project/`.

Runtime modules do not import the governance machinery in `lib/agentic_starter/` or depend on briefs, plans, policies, agent definitions or execution records. The package's declared runtime dependencies and public resources determine what a consumer installs. Product tests may exercise the package and its declared fixtures; methodology tests separately exercise root tooling.

`./bin/setup`, `./bin/test` and `./bin/check all` remain the cwd-independent entry points. They select the root runtime declaration, metadata and lockfile as one atomic contract. Package selection, type checking, CLI smoke checks and dynamic lint/format coverage are preserved through methodology transfers.

A future package-layout change is a separately scoped product decision with an empirical packaging check; this policy does not authorize it.
