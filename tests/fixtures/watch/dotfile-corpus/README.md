# dotfile-corpus

A directory tree with files at every depth carrying dotfile path components
plus their non-dotfile siblings. Used by
`test_dotfile_exclusion_applies_at_every_depth`.

The tree under `contents/` is the watch-root candidate. Tests pass
`contents/` as the watch root and assert every file with a dotfile path
component returns `True` from `IgnoreEngine.is_ignored`, every non-dotfile
sibling returns `False`.

Files (all under `contents/`):

| Path | Expected ignored? | Why |
|---|---|---|
| `README.md` | no | non-dotfile sibling at root |
| `subdir/notes.md` | no | non-dotfile child |
| `.git/HEAD` | yes | dotfile at root |
| `.obsidian/workspace.json` | yes | dotfile at root |
| `.DS_Store` | yes | dotfile file at root |
| `subdir/.envrc` | yes | dotfile at depth 1 |
| `deep/nested/dir/.vscode/settings.json` | yes | dotfile at depth 3 |
