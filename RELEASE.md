# Preparing a release

Version 0.1.0 is the tagged release target; preparation alone does not publish it. The MIT license covers this package's code, not separate model weights, dependencies or operator content. Publication, tagging, stable-compatibility policy and disclosure decisions remain with the owner. No command below uploads artifacts or changes a remote.

## Build and inspect

Use the full development checkout with its pinned managed runtime and lockfile:

```bash
./bin/setup
```

Create a fresh artifact directory outside the checkout:

```bash
PALACE_RELEASE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/palace-release.XXXXXX")
```

Build the source archive and wheel with the pinned backend:

```bash
uv build --managed-python --out-dir "$PALACE_RELEASE_DIR/dist"
```

Inspect every archive member and record artifact SHA-256 hashes. The wheel contains the `palace` package and distribution metadata/license. The source archive has an explicit allowlist of package source, metadata, lock/runtime pins, build ignore rules, README, license, changelog, this checklist selected operational documents and contribution/security/provenance notices. Neither archive should include stores, credentials, private inputs, repository planning, test caches or execution evidence. Package source and documentation can still contain pre-existing disclosures; an allowlist does not establish safe publication.

Rebuild into another fresh output directory and compare artifacts. Record actual tool/runtime versions and any nondeterminism; never infer reproducibility from one successful build. The source archive is scoped package-build input, not a complete development checkout with test wrappers, hooks and service templates.

## Clean installation

Export exact runtime requirements from the committed lockfile:

```bash
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file "$PALACE_RELEASE_DIR/requirements.txt"
```

Create an outside-checkout environment:

```bash
uv venv --managed-python --python 3.12 "$PALACE_RELEASE_DIR/venv"
```

Install the hash-bound runtime dependencies, then the inspected wheel without resolving another dependency set:

```bash
uv pip install --python "$PALACE_RELEASE_DIR/venv/bin/python" --require-hashes -r "$PALACE_RELEASE_DIR/requirements.txt"
```

```bash
uv pip install --python "$PALACE_RELEASE_DIR/venv/bin/python" --no-deps "$PALACE_RELEASE_DIR/dist/palace-0.1.0-py3-none-any.whl"
```

Move outside the checkout before checking import provenance:

```bash
cd "$PALACE_RELEASE_DIR"
```

```bash
./venv/bin/python -c 'import palace; print(palace.__version__, palace.__file__)'
```

Expect version `0.1.0` and a module path inside this new environment. Inspect the installed CLI and provider registry:

```bash
./venv/bin/palace --help
```

```bash
./venv/bin/palace providers list
```

Smoke-test synchronous indexing and retrieval on synthetic data with an explicitly injected deterministic embedder and reranker, or use the checkout's documented disposable HTTPS walkthrough. Do not use a production corpus or paid inference to prove packaging. Verify a matching result, recorded embedding identity, origin-preserving multi-store retrieval, and loud refusal of an incompatible identity. Do not install or restart daemons during a package smoke.

Repeat the exact-wheel installation and synthetic smoke on each claimed platform. Current qualification covers macOS ARM64 and Linux ARM64; it does not establish x86, Linux daemons, production latency or provider quality. Maintainers also run the full repository check sequence and final handoff gate against the actual delivery candidate. The wheel does not include checkout launchd templates, capture-hook scripts or demo launchers; those integrations require a full development checkout.

## Owner publication checklist

- Ratify the version and supported scope, including whether and when the greenfield compatibility policy ends.
- Review current-source, built-artifact and reachable-history disclosure findings. Unresolved disclosure blocks public release; consumer references must be absent from current files and built artifacts; historical copies must be removed before public release under separately authorized Git-history work.
- Inspect the exact artifacts, license, dependency/model licenses, metadata and clean-install evidence; accept their hashes as the publication inputs.
- Judge the outstanding multi-store and private-provider walkthroughs separately from automated correctness.
- Select any tag, distribution channel or repository visibility change explicitly. Keep the approved commit, artifact hashes, platform/tool versions, gate receipts and disclosure disposition with the release record.

Preparation is not publication. A bounded scan can identify disclosures but cannot prove the absence of all secrets or sensitive history. Other copies and unreachable history remain outside its claim.
