---
slug: pygmy-quoll
title: Review bounded indexing and recovery behaviour
status: pending
category: product-review
urgency: low
blocks:
  - Human product/readability acceptance of bounded indexing and recovery
filed: 2026-10-20
needed_at: after delivery
source: kickoff
refs:
  - plan/phase-21.md
  - palace/index/README.md
---

The implementation's automated gates do not settle product/readability judgment. This protocol uses disposable sources and a fake embedder. It does not change a production store or restart a service. Confirm whether the visible progress, refusal wording and manual recovery guidance are useful, then report any desired changes. Delivery does not close this action.

Run from the repository root. The first check exercises the actual daemon with fake providers, idle/full queues, successful-prefix cursor preservation and terminal failure parking:

```sh
./bin/test tests/index/test_scheduler.py tests/index/test_index_server.py -q
```

Expected: four proof families pass. For visible synchronous progress, run this single command; its temporary directory is removed on exit:

```sh
./bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from palace.index._errors import IndexError
from palace.index.build import build
from tests.index.conftest import FakeEmbedder
from tests.index.test_scheduler import _document, _limits

class Interrupted(Exception):
    pass

with TemporaryDirectory(prefix='palace-bounded-demo-') as directory:
    base = Path(directory)
    store, root = base / 'store', base / 'sources'
    root.mkdir()
    (root/'a-large.md').write_text(_document(30))
    (root/'z-small.md').write_text('## Small\nsearchable small document\n')

    def interrupt(item):
        if item.event == 'receipt':
            raise Interrupted()

    try:
        build(store=store, watch_root_filter=root, full=True,
              embedder=FakeEmbedder(), limits=_limits(), progress=interrupt, log=lambda _: None)
    except Interrupted:
        print('Interrupted after one durable turn; no document was partially published.')

    def observe(item):
        if item.event in {'reused', 'committed', 'complete'}:
            print(item.event, item.path, 'committed=', item.committed, 'reused=', item.reused)

    resumed = FakeEmbedder()
    build(store=store, watch_root_filter=root, full=True,
          embedder=resumed, limits=_limits(), progress=observe, log=lambda _: None)
    assert resumed.call_count == 30
    (root/'z-small.md').write_text('x' * (_limits().source_bytes + 1))
    try:
        build(store=store, watch_root_filter=root, embedder=FakeEmbedder(),
              limits=_limits(), log=lambda _: None)
    except IndexError as error:
        print('Visible refusal:', error)
    else:
        raise AssertionError('Oversized source unexpectedly admitted')
PY
```

Expected: an interruption after one durable turn; a `reused a-large.md` observation; `z-small.md` committed before `a-large.md`; then an explicit oversized-source refusal stating that old rows remain visible. Judge whether that sequence and the limits/recovery documentation explain the behaviour adequately.

The daemon deliberately parks after any failed event. The executable witness proves the last successful checkpoint and that later events do not commit. Do not restart or mutate a production daemon for this review. On real work failure, remedy the source/provider/lock cause and restart explicitly; changing an ignore rule does not skip an already-logged event.

The invocation freezes source snapshots. Editing a deferred source can refuse the build; rerun replans while retaining compatible receipts. Corrupt derived receipts stop for inspection rather than disappearing automatically. Default limits are engineering admission choices, not measured performance, memory, physical disk or cost guarantees. These tradeoffs remain part of the operator's product review.
