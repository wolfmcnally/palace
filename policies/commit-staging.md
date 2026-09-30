# Policy: Commit Staging Integrity

Authority mode follows [four-canonical-agents.md](four-canonical-agents.md). Product primary mode uses independent advice and primary acceptance; delegated product mode retains approval verdicts and bounded revision loops. References below to reviewer assent or mandatory re-review govern delegated work only. Required objective gates and truthful evidence apply in both modes. All methodology work, including teach/learn, follows [review-lanes.md](review-lanes.md): one-shot by the primary, no delegated production or review, and commit/fast-forward-push authority after required checks.

A staging list is a set of assertions about the repository. Re-verify every
assertion against the tree as it exists at staging time, not as it existed when
the list was composed.

## The rule

1. **Re-check the tree immediately before every commit.** Run
   `git status --porcelain` and read every row. Unexpected `R`, `M`, `A`, or
   `??` entries—and expected entries that are absent—are failures. A path list
   composed earlier in the session is stale by default.
2. **Stage explicit paths.** Never use `git add -A` or `git add .` in a checkout
   another session may share.
3. **Treat explicit paths as necessary, not sufficient.** They have two known
   blind spots:
   - **Shared file.** When two sessions edit one file, staging that path carries
     both sessions' hunks. Partition the file's hunks and identify their owners
     before committing; if that cannot be established safely, park delivery.
   - **Moved path.** A rename or archive move requires both its source and
   destination in the explicit path set. Stage both with deletion-aware,
   path-scoped `git add -A -- <exact paths>`, then verify with rename detection
   disabled so Git's presentation of the pair as one rename cannot hide either
   side or a later destination edit. Repository-wide `git add -A` remains
   forbidden. On a retry after a parked staging attempt, an absent source may
   already be removed from the index; omit only that unstageable source from
   the repeated `git add`, then require the complete source-and-destination set
   in the staged diff exactly as before.
4. **Inspect the staged candidate and the resulting commit.** Read the staged
   diff before committing. Afterward, compare `git show --stat --oneline HEAD`
   with the intended file list. A successful exit proves that Git created a
   commit, not that the commit contains what was intended.

5. **Verify before the push, in its own block.** The post-commit checks —
   `git show --stat`, a clean `git status`, residual-dirt inspection — decide
   whether the commit is fit to publish, so chaining them behind the push in one
   command block runs them after the irreversible step and turns a catchable
   mistake into a published one. Residual modification on a path the commit just
   claimed means the commit is short. This is the delivery case of the rule that
   a command whose refusal or result must be read gets its own block
   (`verification-discipline.md`).

A staged path name is not its content. When a moved file is edited after `git mv` stages it, the index can still hold the pre-edit bytes. Read the staged diff and verify the destination content before committing; the delivery guard and deletion-aware source/destination checks above remain binding.

## Identity across the commit boundary

The gate-bound orchestration identity and commit-bound delivery identity are
two projections of the same candidate. The former includes committed paths so
a staged deletion remains an explicit `missing` entry and cannot disappear
during staging. Immediately before commit, `bin/deliver` also records the
index-target projection (`kickoff-tree-id --delivery`); after commit that
projection must be byte-identical. This is the only identity expected to remain
stable across the `HEAD` transition, because the broader orchestration identity
intentionally changes when a committed deletion ceases to be a missing path.

## Corollaries

- A preservation hold is not self-enforcing. Re-check live tree identity before
  acting on an earlier snapshot.
- Moving or deleting a required contract member updates every independent
  inventory that names it, in the same change.
- If a staging defect reaches history, fix forward with an ordinary commit.
  History rewriting remains on the destructive Git surface owned by the user.

Delivery authority and its park conditions remain governed by
[`human-in-the-loop.md`](human-in-the-loop.md). This policy governs the
integrity of any commit that authority permits.
