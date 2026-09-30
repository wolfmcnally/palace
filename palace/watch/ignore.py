"""Per-watch-root ignore engine.

Implements the two rules from ``briefs/sota-memory-and-recall.md`` §B.1
"Ignore semantics are first-class":

1. **Universal dotfile exclusion.** Any path component starting with ``.`` is
   excluded at every depth. Not configurable. Applies before ``.gitignore``
   semantics even consult the disk.
2. **Per-directory ``.gitignore`` composition.** Per-directory ``.gitignore``
   files are honored across the watch root, composed parent-first so child
   overrides win. The composition follows the full git-wildmatch spec —
   negation patterns (``!keep.log``), anchored vs unanchored, directory-only
   (``build/``), ``*`` vs ``**`` — via :class:`pathspec.GitIgnoreSpec`.
   ``.gitignore`` semantics apply **whether or not** the root is a git
   repository.

API design:

- :func:`is_dotfile_excluded` is a pure function on a ``PurePosixPath``
  *relative to a watch root*; the watch root's absolute path may legitimately
  sit under a directory whose name starts with a dot (the engine never
  consults the root's own absolute path components — only the path's
  relative form).
- :class:`IgnoreEngine` is constructed per watch root; cross-root state is
  not shared. The public :meth:`IgnoreEngine.is_ignored` returns a bool;
  :meth:`IgnoreEngine.reason` returns ``"dotfile"`` / ``"gitignore"`` /
  ``None`` for the inspection CLI.
- :meth:`IgnoreEngine.refresh` is a no-op stub in Phase 2.1. Phase 2.2 will
  wire FSEvents-driven cache invalidation here so callers do not have to
  widen the public API later.

Library choice: ``pathspec.GitIgnoreSpec.from_lines`` per the library's
recommendation in its own docs ("handles edge-cases to more closely replicate
Git's behavior"). The generic ``PathSpec.from_lines("gitignore", ...)``
constructor is the documented alternative; the gitignore-corpus regression
test (``test_gitignore_engine_matches_git_check_ignore_for_representative_corpus``)
pins the chosen API against ``git check-ignore`` ground truth so the choice
is empirically validated.

Performance posture: Phase 2.1 walks parents on every :meth:`is_ignored`
call. Against a watch root with thousands of ``.gitignore`` files this is
O(depth) per call; the overhead is tolerable at single-call granularity
(``palace watch check`` is interactive). Phase 2.2 will introduce a cache
invalidated by FSEvents on the ``.gitignore`` files themselves; the
:meth:`refresh` hook is the seam.

Hand-rolled glob filtering is forbidden by the brief verbatim — every
pattern decision goes through ``pathspec``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from pathspec import GitIgnoreSpec

from palace.watch._errors import WatchError

__all__ = [
    "GITIGNORE_FILENAME",
    "IgnoreEngine",
    "IgnoreReason",
    "is_dotfile_excluded",
]


GITIGNORE_FILENAME: str = ".gitignore"
IgnoreReason = Literal["dotfile", "gitignore"]


def is_dotfile_excluded(relative_path: PurePosixPath) -> bool:
    """True iff any component of ``relative_path`` starts with ``.``.

    ``relative_path`` is the path relative to the watch root. An empty path
    (the root itself) returns ``False`` — the rule excludes *children* with
    dotfile components, not the root itself.
    """
    return any(part.startswith(".") for part in relative_path.parts)


class IgnoreEngine:
    """Classify paths under one watch root via the two ignore rules.

    Construct with the resolved absolute path of a watch root. Call
    :meth:`is_ignored` or :meth:`reason` with the absolute path of a candidate
    child; a candidate outside the root raises :class:`WatchError`. The
    engine is short-lived in Phase 2.1; Phase 2.2 will wire a longer-lived
    cache and invalidate via :meth:`refresh`.
    """

    def __init__(self, root: Path) -> None:
        self.root: Path = root.resolve()

    # ------------------------------------------------------------------ public

    def is_ignored(self, absolute_path: Path) -> bool:
        """True iff ``absolute_path`` is excluded under this watch root."""
        return self.reason(absolute_path) is not None

    def reason(self, absolute_path: Path) -> IgnoreReason | None:
        """Classify ``absolute_path`` as ``"dotfile"`` / ``"gitignore"`` / not-ignored.

        Dotfile exclusion wins ahead of ``.gitignore`` semantics — a file
        inside ``.git/`` reports ``"dotfile"`` even when no ``.gitignore``
        names it. This matches the brief's "applies at every depth" framing
        and keeps the CLI's ``reason:`` annotation stable.
        """
        relative = self._relative_to_root(absolute_path)
        if is_dotfile_excluded(relative):
            return "dotfile"
        if self._gitignore_match(absolute_path, relative):
            return "gitignore"
        return None

    def refresh(self) -> None:
        """Invalidate any internal cache.

        Phase 2.1 does naive walk-parents-each-call, so there is nothing to
        invalidate. Phase 2.2 will wire FSEvents-driven cache invalidation
        here; shipping the method now keeps that surface stable.
        """
        return None

    # ------------------------------------------------------------------ internals

    def _relative_to_root(self, absolute_path: Path) -> PurePosixPath:
        candidate = absolute_path.resolve() if absolute_path.exists() else absolute_path
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise WatchError(
                f"path is not under watch root: {absolute_path} not under {self.root}"
            ) from exc
        return PurePosixPath(*relative.parts)

    def _gitignore_match(self, absolute_path: Path, relative: PurePosixPath) -> bool:
        """Apply per-directory ``.gitignore`` composition, parent-first.

        For each ancestor of ``absolute_path`` from the watch root down to its
        immediate parent, load that directory's ``.gitignore`` (if present)
        and match the *path relative to that directory*. Per the
        git-wildmatch spec, a child ``.gitignore`` extends (and may negate)
        the parent's patterns; later matches win. A child match overrides a
        parent verdict; a child non-match does not unset a parent ignore
        unless an explicit negation pattern is present (this is the
        documented gitignore semantic, and is what :class:`GitIgnoreSpec`
        implements).
        """
        if not relative.parts:
            return False

        # Determine whether to ask gitignore as a directory or as a file. Some
        # patterns are directory-only (`build/`); pathspec needs the trailing
        # slash on the queried path to honor that. Bias toward the live
        # filesystem state; tests pass paths that exist on disk.
        is_directory = absolute_path.is_dir() if absolute_path.exists() else False

        # Walk from the watch root down, accumulating spec lines parent-first.
        # ``relative.parts[:-1]`` is the chain of containing directories of
        # the candidate (excluding the candidate itself).
        ignored = False
        relative_parts = relative.parts
        cumulative = PurePosixPath(*relative_parts)

        # For each ancestor directory inside the watch root (including the
        # root itself), load that .gitignore and match the path *relative to
        # that directory*. Parent first, child overrides.
        for depth in range(len(relative_parts)):
            ancestor_parts = relative_parts[:depth]
            ancestor_dir = self.root.joinpath(*ancestor_parts)
            gitignore = ancestor_dir / GITIGNORE_FILENAME
            if not gitignore.is_file():
                continue
            spec = self._read_spec(gitignore)
            # Path relative to this ancestor directory.
            sub = PurePosixPath(*cumulative.parts[depth:])
            sub_query = sub.as_posix()
            if is_directory and not sub_query.endswith("/"):
                sub_query = sub_query + "/"
            verdict = self._match_with_spec(spec, sub_query)
            if verdict is True:
                ignored = True
            elif verdict is False:
                # Explicit negation flips the running verdict back to
                # not-ignored.
                ignored = False
            # ``None`` means the spec did not address this path at this
            # level; leave the running verdict alone.
        return ignored

    @staticmethod
    def _read_spec(gitignore: Path) -> GitIgnoreSpec:
        # Read fresh on every call. Phase 2.2 will memoize.
        try:
            text = gitignore.read_text(encoding="utf-8")
        except OSError:
            return GitIgnoreSpec.from_lines([])
        return GitIgnoreSpec.from_lines(text.splitlines())

    @staticmethod
    def _match_with_spec(spec: GitIgnoreSpec, query: str) -> bool | None:
        """Return ``True``/``False``/``None`` for ignored / negated / unaddressed.

        ``pathspec``'s ``match_file`` returns ``True`` for ignored and
        ``False`` for not-addressed *or* negated. To distinguish negation
        from unaddressed we ask the spec twice: once for the actual query
        and once for the query without negation patterns. If the spec
        matches as ignored, we report ``True``. Otherwise we walk the
        spec's individual patterns to detect an explicit negation; finding
        one returns ``False``, otherwise ``None``.
        """
        if spec.match_file(query):
            return True
        # Inspect patterns to detect explicit negation against the query.
        # pathspec represents a negation as include=False; the per-pattern
        # match_file returns a truthy match object when the negation
        # addresses the query.
        for pattern in spec.patterns:
            include = getattr(pattern, "include", None)
            if include is False and pattern.match_file(query):
                return False
        return None
