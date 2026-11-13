# Preparing a release

This checklist governs the 0.5.1 packages; building them alone does not publish a release. Preserve the historical v0.1.0 tag and assets. The MIT license covers this package's code, not separate model weights, dependencies or operator content. Publication, tagging, stable-compatibility policy and disclosure decisions remain with the owner. No command below uploads artifacts or changes a remote.

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

Export exact runtime requirements from the committed lockfile. Conric names its published Git release tag, which a hash-checked requirements file cannot carry, so it is excluded here and installed separately below. The lockfile retains the resolved commit as its integrity anchor:

```bash
uv export --frozen --no-dev --no-emit-project --no-emit-package conric --format requirements-txt --output-file "$PALACE_RELEASE_DIR/requirements.txt"
```

Create an outside-checkout environment:

```bash
uv venv --managed-python --python 3.12 "$PALACE_RELEASE_DIR/venv"
```

Install the hash-bound runtime dependencies, then the inspected wheel without resolving another dependency set:

```bash
uv pip install --python "$PALACE_RELEASE_DIR/venv/bin/python" --require-hashes -r "$PALACE_RELEASE_DIR/requirements.txt"
```

Install Conric from its published v0.1.0 tag without dependency resolution; verify the tag still resolves to the lockfile commit. Its only dependency, PyYAML, is already hash-installed above:

```bash
uv pip install --python "$PALACE_RELEASE_DIR/venv/bin/python" --no-deps "conric @ git+https://github.com/wolfmcnally/conric.git@v0.1.0"
```

```bash
uv pip install --python "$PALACE_RELEASE_DIR/venv/bin/python" --no-deps "$PALACE_RELEASE_DIR/dist/palace-0.5.1-py3-none-any.whl"
```

Move outside the checkout before checking import provenance:

```bash
cd "$PALACE_RELEASE_DIR"
```

```bash
./venv/bin/python -c 'import palace; print(palace.__version__, palace.__file__)'
```

Expect version `0.5.1` and a module path inside this new environment. Inspect the installed CLI and provider registry:

```bash
./venv/bin/palace --help
```

```bash
./venv/bin/palace providers list
```

Smoke-test synchronous indexing and retrieval on synthetic data with an explicitly injected deterministic embedder and reranker, or use the checkout's documented disposable HTTPS walkthrough. Do not use a production corpus or paid inference to prove packaging. Verify a matching result, recorded embedding identity, origin-preserving multi-store retrieval, and loud refusal of an incompatible identity. Do not install or restart daemons during a package smoke.

Repeat the exact-wheel installation and synthetic smoke on each claimed platform. Current qualification covers macOS ARM64 and Linux ARM64; it does not establish x86, Linux daemons, production latency or provider quality. Maintainers also run the full repository check sequence and final handoff gate against the actual delivery candidate.

## Native watcher qualification

Release qualification adds the mandatory native lane. On the qualifying macOS host, on the clean delivered release commit, run these in order:

```bash
./bin/check all
```

```bash
./bin/check native
```

```bash
./bin/check release
```

A tag requires `CHECK RELEASE VERIFIED`. A failed native run blocks the release and keeps its log and run record; `release` refuses until the most recent native run for that exact candidate on this host is a passing one, and it names every earlier failure. A later run needs operator or coordinator authority and is never automatic. A native receipt from another host never qualifies. The continuous-integration native step on the macOS runner is a detector after push: it never gates delivery and is not a release receipt. The wheel does not include checkout launchd templates, capture-hook scripts or demo launchers; those integrations require a full development checkout.

## Dogfood the release

A published release is not running on the publishing host until its supervised services run it. The editable system tool and the launchd daemons are separate: the daemons run `uv run palace … serve` from this checkout, and a running process keeps the code it started with until it restarts. After publication, on the published commit:

1. Reinstall the editable tool against the lock, as `CLAUDE.md` describes, and confirm that `palace --version` reports the release.
2. Confirm with read-only commands that the release opens the operator's existing store without refusal: `palace index status`, `palace index embedder show` and `palace index vector-backend show` report the stamped identity. Take a non-destructive backup of the derived index first, for example with SQLite's online `.backup`.
3. Confirm that each installed LaunchAgent plist still equals its rendered tracked template under `daemons/`, then restart each supervised daemon in turn with `launchctl kickstart -k gui/$(id -u)/<label>`.
4. Verify the running release, not only liveness. Check for new process start times, exactly one instance of each daemon, the version reported by the capture daemon's `/health`, and clean startup log lines. Then confirm that a real change flowed from the watched root through reindex and index. Never write to the watched root to manufacture that evidence; wait for an ordinary change.

## Owner publication checklist

- Ratify the version and supported scope, including whether and when the greenfield compatibility policy ends.
- Review current-source, built-artifact and reachable-history disclosure findings. Unresolved disclosure blocks public release; consumer references must be absent from current files and built artifacts; historical copies must be removed before public release under separately authorized Git-history work.
- Inspect the exact artifacts, license, dependency/model licenses, metadata and clean-install evidence; accept their hashes as the publication inputs.
- Judge the outstanding multi-store and private-provider walkthroughs separately from automated correctness.
- Select any tag, distribution channel or repository visibility change explicitly. Keep the approved commit, artifact hashes, platform/tool versions, gate receipts and disclosure disposition with the release record.

Preparation is not publication. A bounded scan can identify disclosures but cannot prove the absence of all secrets or sensitive history. Other copies and unreachable history remain outside its claim.
