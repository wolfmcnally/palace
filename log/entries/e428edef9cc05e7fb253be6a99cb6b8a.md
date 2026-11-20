---
{"at": "import", "body_sha256": "01fdc5ca7ecd8b73b2a7897559091fb5558536c046342f1c238815e19436ac49", "id": "e428edef9cc05e7fb253be6a99cb6b8a", "previous": "e9b4f79b82145bdcb0e23cd4f7440177", "sequence": 128, "source_offset": 395173, "writer": "unattributed-import"}
---
## 2026-11-19 21:30 — END
Phase 36 — Release 0.7.0 with the opt-in vector cache

Execution trace: c0277e904fa84475a7b12a8734dd5887

Palace 0.7.0 is published. The annotated tag v0.7.0 and the GitHub release point at the qualified commit, and the public assets re-hash to the qualified archives. A deployed consumer can now pin v0.7.0 and opt in to the vector cache; the default search is unchanged. Not done: consumer integration (queued separately by the coordinator), Linux ARM64 qualification, and the dogfood restart of this host's supervised daemons.

Release record:
- Release commit 472e76f0f3a96430a45b60ae0d4dec57fc6872ed ("Prepare Palace 0.7.0 release"), on Phase 35's cd3ac34258389b5087acf202289052c8297de5af. Both commits are unsigned, with the repository's personal author and committer identity set per command under the operator's 2026-11-17 direction; no git configuration was changed.
- The first full gate on the release tree failed: `tests/test_smoke.py` still expected `palace 0.6.0`, written as a regular expression that my literal version search missed although Phase 34's release commit changed the same file. Fixed before delivery; that gate's result was also misreported as success by a trailing `echo` in the background command, caught by reading the log.
- Qualification on the clean 472e76f: ./bin/check all PASS on candidate cf99a931133abdfe5341e841582ecccec628746745f86e2d9ed8e216188ff766 (318 passed); CHECK NATIVE PASS (6 passed, 4 skipped by the 2026-11-12 ruling); CHECK RELEASE VERIFIED, native-failed-runs=0.
- Packages: wheel palace-0.7.0-py3-none-any.whl SHA-256 15c0ba7963e57058925d0d7315c9f943f05fa21cf30f1e3348cab67fa742192d; source archive palace-0.7.0.tar.gz SHA-256 2c9a7934a1c6d1a8ae72eec09e2269e2cb15e3ec01e336a96fd535c97289c80e. Two builds and a rebuild from `git archive 472e76f` are byte-identical (uv 0.9.26). The source archive now carries `docs/vector-cache.md`. A hash-locked clean install and the package smoke, which now includes a vector cache parity check from the installed wheel, pass on macOS ARM64. Linux ARM64 was not run; hosted CI's Linux job is x86_64. The disclosure screen of both archives found no consumer name.
- Branch CI run 44616290708 (472e76f) and tag CI run 44617643541 (v0.7.0) passed on ubuntu-latest and macos-latest.
- Tag: annotated v0.7.0, tag object 3fb719a7311858b041eb02ee537a599e8213e5fb, tagger Wolf McNally, message "Palace 0.7.0", unsigned like v0.6.0. `git ls-remote` shows the tag peeling to 472e76f; v0.6.0 is unchanged.
- GitHub release v0.7.0, published 2026-11-19T21:29:33Z, not draft or prerelease, with the wheel, the source archive and SHA256SUMS. Anonymous downloads re-hash equal and SHA256SUMS is byte-identical to the qualified one. A fresh hash-locked install of the downloaded wheel passes the package smoke, Conric resolving to the locked commit.

Review: the Phase 35 code advisory and its dispositions; no further reviewer, by the coordinator's direction.

Acceptance:
- Objective: the tag resolves to the qualified commit on the remote; the downloaded assets re-hash equal; native and release checks passed on the delivered commit.
- Parked for the user: consumer enablement and its `on_unavailable` setting (recommended `fallback`); Linux ARM64 qualification; representative-corpus measurement; the dogfood restart; publication judgment beyond the scoped tag.

Delivery: this record by guarded commit and fast-forward push after the handoff gate.

Ripple: none — no downstream sketches.

Lessons:
- filed: none.
- occurrences added: none. The missed version regex is a release-procedure slip with a durable home in `RELEASE.md`'s version list rather than a new lesson; it is recorded above.
- graduation DECIDE: unchanged.
