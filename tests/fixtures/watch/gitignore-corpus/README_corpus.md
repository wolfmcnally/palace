# A non-dotfile readme inside the corpus to exercise the not-ignored path.
This file is **not** matched by any pattern in `.gitignore`. Tests assert
that `IgnoreEngine.is_ignored` returns False for it.
