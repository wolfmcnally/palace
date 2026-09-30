# Platform qualification

As of 2026-09-06, the supported Linux scope is synchronous indexing, portable index artifacts, search and evaluation. Qualification uses Python selected by the root runtime pin and the committed lockfile, through the same repository entry points on macOS and Linux.

| Surface | Qualification contract |
| --- | --- |
| Synchronous index build and incremental reconciliation | Controlled local corpus, deterministic embedding fixtures, deletion reconciliation and unchanged-file checks on both platforms |
| Portable artifact reading, search and evaluation | SQLite/FTS5/sqlite-vec artifacts and hermetic provider fixtures on both platforms; no live inference required |
| FSEvents watcher | macOS only; Linux refuses before native-extension loading, directory creation or background threads; use `palace index build` for synchronous ingestion |
| LaunchAgent install, uninstall and restart | macOS only; Linux refuses before reading templates, changing files or invoking `launchctl` |
| Apple-specific model serving | Outside Linux qualification; no Apple serving runtime is imported or provisioned by the synchronous package. Local ONNX client code and fixture-based provider tests do not establish a live serving deployment. |

The host must provide `uv`, Git, Bash, `curl`, `jq` and OpenSSL. Python dependencies come from the lockfile; `jq` and `curl` are also required by the retained capture-hook integration tests; OpenSSL creates temporary certificates for hermetic private-provider TLS tests. A minimal Debian/Ubuntu container needs those operating-system packages provisioned before running the gate; do not skip hook tests when they are absent.

Run the complete retained estate on each platform from the checkout:

```sh
./bin/setup
```

```sh
./bin/check all
```

On a workstation with the separately installed `devlinux` qualification lane, the equivalent Linux command is:

```sh
devlinux palace ./bin/check all
```

The lane uses an isolated Linux environment and the same checkout and lockfile. Its ARM64 result does not establish x86 support. It requires Docker Desktop; an unavailable lane is an unverified result.

Ten positive watcher integration tests require native FSEvents and are explicitly excluded on Linux. The CLI smoke proof executes the macOS-operation refusals on both platforms and checks preservation of an existing plist and absence of a new store. Hermetic LaunchAgent template tests use an explicit Darwin platform fixture while keeping all writes in temporary directories and all service calls stubbed. This proves template behavior, not Linux daemon support.

No production corpus rebuild, remote model request, retrieval retuning or live provider benchmark belongs to this gate. Runtime output and local candidate-bound receipts retain the actual pass/failure and complete diagnostics; this capability document is not a substitute for them.
