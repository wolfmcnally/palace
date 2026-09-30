# daemons/

Long-running palace processes managed by `launchd`. Each daemon lives in its own subdirectory alongside its committed `.plist` file (launchd plists are infrastructure-as-code per `CLAUDE.md`'s architectural invariants).

Subdirectories:

- `daemons/capture/` — Phase 1: Stop-hook session capture daemon. Phase 1.3 lands the LaunchAgent plist (`ai.palace.capture.plist`) and operator README.
- `daemons/reindex/` — Phase 2: FSEvents-driven incremental reindexer. Phase 2.1 lands the committed reference shape of the watch-roots config (`watch-roots.example.toml`) plus this directory's README; the launchd plist and the daemon's runtime entry point land in Phase 2.2 onwards.

Planned but not yet present:

- `daemons/consolidate/` — Phase 6: Consolidation / dreaming job.
