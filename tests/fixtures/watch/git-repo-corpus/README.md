# git-repo-corpus

A directory tree that DOES contain a `.git/` directory (committed as a
regular directory with a `HEAD` file inside; NOT a real submodule). Used by
`test_dotfile_exclusion_applies_in_git_repo`.

The fixture's `.git/HEAD` content is the literal string `ref: refs/heads/main`
(LF-terminated) so the corpus is a syntactically-valid git-shaped directory
without being a live git checkout.

Tests pass this directory as the watch root and assert that
`IgnoreEngine.is_ignored(<root>/.git/HEAD)` returns `True` — the dotfile rule
applies inside git-shaped roots the same way it applies to plain directories.
