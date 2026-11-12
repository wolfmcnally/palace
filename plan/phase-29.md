---
id: '29'
title: Generic operator wording in source and operations docs
depends_on:
  - phase-25.md
informs: []
review_lane: full
---

# Generic operator wording in source and operations docs

## Commission

Authorized on 2026-11-11 through the authenticated factotum as a forward-only privacy cleanup of the current tree, under the root invariant that durable role language names the user, operator or owner (they/them) and that upstream artifacts carry no personal or consumer-identifying detail. Several runtime docstrings, comments, a CLI help string, operations READMEs, one smoke-script comment and the consolidator's extraction prompt still name the project author as the person who runs Palace. Authorship credit is a different thing and stays.

The phase changes wording and the extraction prompt only. Behavior, defaults, storage layout, file formats, public command surfaces and every configured value stay as they are. History is not rewritten. Both review and evidence lanes are full, with independent Opus plan review and code critique.

**Carried working-tree changes are not this phase's.** The tree carries Phase 26's and Phase 28's uncommitted, parked test changes in `tests/reindex/test_server.py` and `tests/proof-estate.yaml`, plus their phase files, ledger rows, log entries and lessons. Those belong to their own parked phases. This phase does not edit `tests/reindex/test_server.py`, does not stage or claim any Phase 26 or 28 change, and does not accept, resume or reinterpret either park or its receipts. Where this phase must touch `tests/proof-estate.yaml`, it owns only its own entry.

## Goal

The current tree's runtime source and operations documentation describe the person running Palace generically, and the consolidator's extraction prompt asks for durable facts about the user, their projects or their decisions, proved by a synthetic test that captures the prompt without any provider call.

## Deliverables

- Operator-role wording in runtime docstrings, comments and help text under `palace/` replaced with generic role language (the user, the operator, the store owner; they/them), including the package and CLI product description. Authorship credit elsewhere is unchanged.
- The extraction system prompt in `palace/consolidate/llm.py` asks for durable facts about the user, their projects or their decisions, keeping its existing criterion and its exclusion of transient task chatter.
- One synthetic focused test that drives the consolidator's Ollama client through an injected `httpx.MockTransport`, captures the outgoing chat request, and asserts the generic system prompt and the absence of a personal name. No network, model or provider call.
- A prepared proof-estate admission record for that test with its contract, independent oracle, red witness, non-subsumption account and the exact budget request. The positive budget is an operator approval; no unrelated proof is retired to fund it.
- Generic wording in the operations READMEs under `daemons/` and `palace/`, and in the comment of `bin/palace-scaffold-vault-smoke`. Default path values in those documents stay as written.
- A staged-diff and source audit showing the remaining name occurrences are only authorship, maintainer contact, dependency URLs, governing prose deferred below, and append-only history.

## Acceptance

- A repository search of the tracked tree, excluding append-only history (`LOG.md`, `EXECUTION_LOG.jsonl`, `lessons/`, `reports/`), license and provenance files and the lockfile, finds the author's personal name only in authorship credit, maintainer contact, dependency URLs, the random user-action slug, and the governing prose and helper identifier listed under Exclusions.
- The new prompt test fails against the pre-phase prompt (red witness) and passes after the change, with no outbound request.
- Every changed Python file passes lint, format and type checks, and the focused consolidate tests pass unchanged apart from the new test.
- `./bin/test-governance validate` passes with the admission record and its approved budget, and without any unrelated retirement.
- No configured value, default, schema, CLI option name or file format changes; the diff touches docstrings, comments, help strings, Markdown prose, one prompt string and the new test only.
- The full-gate and delivery path is reported to the coordinator before any aggregate gate attempt, because the tree also carries the parked Phase 26 and 28 changes, including native watcher proofs whose last full gate failed. Implementation, focused checks and critique may proceed first.
- Manual: whether the generic wording reads well belongs to the operator.

User Demo: N/A — wording and one prompt string, with no new product surface.

## Exclusions

- Governing prose. Name occurrences in `policies/` and `briefs/` are a named separate follow-on that uses the prepared-park and fresh-capture route. That follow-on also carries the rename of the sessions authored-predicate helper in `palace/consolidate/sources.py`, because `policies/consolidation.md` cites that helper by name; the code identifier and its policy citation move together. Its docstring and comment wording change here.
- The fixed day-partition timezone. Its constant and every use stay as they are; it is storage behavior, not wording. Whether it becomes a declared store convention is a separate operator decision. Only a docstring that describes it in personal terms is reworded here.
- Authorship and maintainer identity: the README credit, package author metadata, the security contact, license files, dependency URLs and their pinned references.
- Documented default paths and their values.
- Append-only history: `LOG.md`, `EXECUTION_LOG.jsonl`, lessons, reports, earlier phase files and commit history.
- Native watcher proofs, their qualification and the release hold.

## Brief refs

None. Authority is the root product invariants in `CLAUDE.md` and the staging and human-judgment policies.
