# gitignore-corpus

A directory tree exercising the slice of git-wildmatch palace must honor:

- Root-level `*.log` unanchored pattern → matches at every depth.
- Root-level negation `!keep.log` re-including a previously-excluded file.
- Anchored pattern `/anchored_root_only.txt` matching only at the root level.
- Directory-only pattern `build/` excluding the directory and its contents.
- Double-star `**/double_star_match.txt` matching at any depth.
- A nested `.gitignore` extending the parent's patterns.

This corpus is **not** a git repository — it is a plain directory tree with
`.gitignore` files, so the test `test_gitignore_engine_applies_in_non_git_root`
exercises the brief's "applies whether or not the root is a git repository"
rule directly against this tree.

`EXPECTED.json` carries the ground-truth expected verdict for every file in
the corpus; the verdicts were captured by running `git check-ignore` against
a transient git-init of the corpus during fixture authoring. Regenerate via:

```sh
TMP=$(mktemp -d)
cp -r tests/fixtures/watch/gitignore-corpus/. "$TMP/"
rm -f "$TMP/EXPECTED.json" "$TMP/README.md" "$TMP/README_corpus.md"
( cd "$TMP" && git init -q && git add -A )
( cd "$TMP" && find . -type f ! -path './.git/*' -print ) \
  | sed 's|^\./||' \
  | sort \
  | while read -r p; do
      ( cd "$TMP" && git check-ignore -v -- "$p" >/dev/null 2>&1 ) \
        && ig=true || ig=false
      printf '%s\t%s\n' "$p" "$ig"
    done \
  | python3 -c 'import json, sys
rows = []
for line in sys.stdin:
    path, ig = line.rstrip("\n").split("\t")
    rows.append({"path": path, "ignored": ig == "true"})
print(json.dumps(rows, indent=2))' \
  > tests/fixtures/watch/gitignore-corpus/EXPECTED.json
rm -rf "$TMP"
```

`git check-ignore` honors the in-repo `.gitignore` files as it normally
would; we do **not** pass `--no-index` (that flag toggles the matcher to
treat paths as if they were absent from the index, which causes
`!keep.log` to be misreported as ignored).

The `.gitignore` file itself is not classified as ignored by the gitignore
rules — but palace's `IgnoreEngine` reports it as `reason: dotfile` because
its filename begins with `.`. Tests assert the `IgnoreEngine` verdict equals
the EXPECTED gitignore-only verdict where the file is non-dotfile, and equal
to `reason: dotfile` for `.gitignore` itself.
