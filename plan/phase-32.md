---
id: '32'
title: Unloadable front matter and a bounded indexed-root lookup
depends_on:
  - phase-31.md
informs: []
review_lane: full
---

# Unloadable front matter and a bounded indexed-root lookup

## Commission

Authorized on 2026-11-12 by the operator's root, relayed through the authenticated factotum, as bounded routine fixes with tests and the required review, preserving existing behavior and data, delivered without a release. Two downstream consumers reported, and the primary independently reproduced:

- A Markdown note whose front matter holds a calendar-invalid date, such as `date: 2026-02-30`, stops indexing. PyYAML's safe loader builds the date with Python's `datetime`, which raises `ValueError`, not `yaml.YAMLError`, so the existing malformed-front-matter catch does not apply; the error escapes the parser and the index daemon parks at that event. Other loader failures escape the same way: an out-of-range time or offset, an integer literal past Python's digit limit or an invalid explicitly tagged number (`ValueError`); an explicit `!!bool`, `!!int` or `!!float` tag on an unrecognized or empty value (`KeyError`, `IndexError`); an explicit `!!timestamp` tag on a non-timestamp (`AttributeError`); a long sexagesimal float (`OverflowError`); and nesting past the interpreter's recursion limit (`RecursionError`). The same narrow catch exists at the capture and fact parsers. A first light-lane run was parked when the code critique showed both that the lane was inadmissible for an input-parsing change and that the first catch missed the tagged-value classes.
- Every search admission, and each store in a multi-store search, runs `SELECT DISTINCT watch_root FROM chunks`, which visits every stored chunk: about three virtual-machine steps per chunk, roughly 17 ms at one million chunks on the qualifying host. The same statement appears in the remote-vector publication check and the full-build remaining-roots check.

The consumer that reported the parser failure confirmed that dropping the whole front matter for such a note, under the existing malformed contract, meets its need; no partial or literal-string fallback is wanted. The consumer that reported the search cost confirmed that no caller depends on the order of the returned roots: each one iterates them and refuses on any overlap, so ascending order only makes the first-named refusal deterministic.

## Goal

A note whose front matter cannot be loaded is indexed from its body with empty front matter and a single attributed stderr line, exactly as syntactically malformed YAML already is, and indexing continues past it. Capture and fact parsing name the same failures as their existing attributed errors. Listing the distinct indexed watch roots costs a number of index probes proportional to the number of roots, independent of the number of chunks.

## Deliverables

- One shared definition of the front-matter load failures (`yaml.YAMLError` and the Python exception classes the safe loader lets escape for input: `ValueError`, `LookupError`, `AttributeError`, `ArithmeticError`, `RecursionError`), caught only around the load call and used by the Markdown index parser, the capture parser and the fact parser.
- The Markdown index parser treats every such failure as malformed front matter; the capture and fact parsers raise their existing attributed errors for it.
- One shared indexed-watch-root lookup that walks the existing `watch_root` index with a recursive skip-scan, replacing the four `SELECT DISTINCT watch_root FROM chunks` statements in single-store search admission, multi-store admission, the remote-vector publication check and the full-build remaining-roots check.
- Focused tests: each failure class at each parser; a store indexing a note with an invalid date and a valid note, both indexed; lookup equality with the `DISTINCT` statement across empty, single-root, many-root, prefix-related, case-distinct and non-ASCII roots; and a falsifying bound showing the lookup's virtual-machine steps do not grow with the chunk count.
- An Unreleased changelog entry and proof-estate admissions for the new proofs.

## Acceptance

- Every existing proof passes unchanged.
- A note with `date: 2026-02-30` in its front matter indexes from its body with empty front matter and one `malformed-frontmatter` stderr line naming its path; a second, valid note in the same store also indexes.
- The capture and fact parsers raise their attributed errors, naming the path for captures, for an invalid date, and never a bare `ValueError` or `RecursionError`.
- The shared lookup returns exactly the sorted result of `SELECT DISTINCT watch_root FROM chunks` on every fixture, and its virtual-machine step count for one root is equal at two chunk counts at least tenfold apart, while the `DISTINCT` statement's count grows.
- No remaining `SELECT DISTINCT watch_root FROM chunks` in the package.
- Focused tests and both full gates pass against their fixed candidates, with one code critique and its primary dispositions retained.
- Manual: whether the fixes meet each consumer's need belongs to the operator and the consumers.

User Demo: N/A — library behavior proved by deterministic synthetic tests.

## Exclusions

- Partial front-matter recovery or a literal-string fallback for an unloadable value.
- Any other query, ranking, retrieval, schema or index change; any new index.
- A release, tag or version change; restarting the operator's daemons.
- Test lanes, gates, policies and the native watcher proofs.

## Brief refs

- [deployed consumer readiness](../briefs/deployed-consumer-readiness.md)
- [store parametric external consumers](../briefs/store-parametric-external-consumers.md)
