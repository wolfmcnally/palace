---
{"at": "import", "body_sha256": "139e90cfe36f1aab140305297b3b6060971ef848fbd7faf050313f3c425f0067", "id": "ac9edc70ea6f5a2aa35a7d5b125cd5a9", "previous": "1684227b902951a0a247295f5fcd7a01", "sequence": 124, "source_offset": 382692, "writer": "unattributed-import"}
---
## 2026-11-17 17:07 — END
Phase 34 — Release 0.6.0 with typed provider throttling

Execution trace: 5a3dd83417224cfebe12f23e441eeb36

Palace 0.6.0 is published. The annotated tag v0.6.0 and the GitHub release point at the qualified commit, and the public assets re-hash to the qualified archives. A downstream library can now pin v0.6.0. Not done: the dogfood restart of this host's supervised daemons, held by the coordinator so other live work on this host is not disturbed.

Release record:
- Release commit 112a582af016840f8055ac97d2dadce7435391dd (correction after source delivery 5a2f785d80111c9e429dcdc35241cadbbd06119b and Phase 33's 8b618c891695f0fcbc91ce582c586b08de04ef1c). Commits 8b618c8 and 5a2f785 carry this host's global git identity (a work address); by the coordinator's resolution that public history is kept as recorded, with no rewrite. 112a582, this record and the tag use the repository's established personal identity, set per command; no git configuration was changed.
- Reviewer F006 (the platform claim) and F007 (the premature close above) closed by the release reviewer on 112a582 at 16:47Z; no remaining review findings.
- Qualification on the clean 112a582: ./bin/check all PASS on candidate 2c4c867539b3cc4326144656d34f2a571231a7d9b2f01bfc97d8acdddae84032 (299 passed); CHECK NATIVE PASS (6 passed, 4 skipped by the 2026-11-12 ruling); CHECK RELEASE VERIFIED, native-failed-runs=0.
- Packages: wheel palace-0.6.0-py3-none-any.whl SHA-256 a6bfa627d844c986ac691461dc9b4e4c79f50b098638dbedac5ba811ddeb7454; source archive palace-0.6.0.tar.gz SHA-256 0d9ffc34f587214e71c19e0633f9bccc926238c5b1c3a6bbf584489142eaa1a3; two builds and a rebuild from `git archive 112a582` byte-identical (uv 0.9.26). Hash-locked clean install and package smoke pass on macOS ARM64. Linux ARM64 was not run for this release; hosted CI's Linux job is x86_64. Bedrock, OpenRouter and pacing proofs pass at boto3/botocore 1.43.90.
- Branch CI run 44128771314 (112a582) and tag CI run 44131689266 (v0.6.0) passed on ubuntu-latest and macos-latest.
- Tag: annotated v0.6.0, tag object 93b08e2766aac7e864f417a4fb74949be96662b4, tagger Wolf McNally, message "Palace 0.6.0", unsigned like v0.5.1 (no signing key is configured). This checkout has no pre-push hook installed, so the push ran no gate of its own; the full gate had passed on the identical tree. `git ls-remote` shows the tag peeling to 112a582; v0.5.1 is unchanged.
- GitHub release v0.6.0, published 2026-11-17T17:06:39Z, not draft or prerelease, with the wheel, the source archive and SHA256SUMS. Anonymous downloads re-hash equal and SHA256SUMS is byte-identical to the qualified one; a fresh hash-locked install of the downloaded wheel passes the package smoke and imports the new API.

Acceptance:
- Objective: the tag resolves to the qualified commit on the remote; the downloaded assets re-hash equal; native and release checks passed on the delivered commit.
- Parked for the user: the dogfood restart (held by the coordinator); Linux ARM64 qualification; whether the contract meets the deployed consumer's need. No live provider throttle was exercised.

Delivery: this record by guarded commit and fast-forward push after the handoff gate.

Ripple: none — no downstream sketches.

Lessons:
- filed: none. The global git identity on this host differing from the repository's established one is recorded above; the coordinator directed per-command identity for future commits rather than a configuration change.
- occurrences added: none.
- graduation DECIDE: unchanged.

