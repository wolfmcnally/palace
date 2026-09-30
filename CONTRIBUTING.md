# Contributing

Use Python 3.12 and uv. Install Git, Bash, curl, jq and OpenSSL. Clone this repository, enter its root, run `./bin/setup`, then `./bin/check all`. No sibling checkout or personal store is required. Tests use synthetic data and guard inference and model downloads; do not replace fixture transports with live services.

Run focused tests with `./bin/test PATH`. Keep full checks green before a pull request. macOS-only integrations are qualified on macOS; Linux must retain explicit refusal behavior. Explain public API or storage changes, update documentation, and preserve local-first boundaries, provenance and provider identity. Contributions are under MIT; submit only material you have the right to contribute.
