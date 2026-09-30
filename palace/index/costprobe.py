"""Hermetic local-cost decomposition for remote embedding throughput.

The probe times the existing serial ``index_one`` seam with four timestamps:
before the call, embed entry, embed exit, and after the call.  The stub
embedder performs no I/O and returns dimension-correct zero vectors, so the
producer and writer measurements contain no network estimate.  The branch
calculation combines those measured local stages with the separately recorded
50 ms/input network figure from ``briefs/remote-embedding-throughput.md``.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from palace.index._errors import IndexError
from palace.index.config import EMBED_DIM, prepare_chunks_db_path
from palace.index.core import index_one
from palace.index.egress import append_record, build_record
from palace.index.embedder_config import resolve_identity
from palace.index.pipeline import classify_kind
from palace.index.schema import init_db
from palace.reindex.bootstrap import enumerate_root
from palace.watch.ignore import IgnoreEngine

__all__ = [
    "BRANCH_THRESHOLD",
    "CEILING_REPETITIONS",
    "Decomposition",
    "MEASURED_UPSTREAM_MULTIPLE",
    "PassTiming",
    "RECORDED_NETWORK_MS_PER_INPUT",
    "StubEmbedder",
    "SyntheticCorpusSpec",
    "ceiling_arch",
    "ceiling_offthread_comparison",
    "generate_corpus",
    "main",
    "measure_audit",
    "measure_decomposition",
    "measure_pass",
    "producer_thread_headroom",
    "render_markdown",
    "reported_ceiling",
    "select_branch",
]


BRANCH_THRESHOLD: float = 2.0
CEILING_REPETITIONS: int = 5
# Recorded in briefs/remote-embedding-throughput.md section 2.3.
RECORDED_NETWORK_MS_PER_INPUT: float = 50.0
MEASURED_UPSTREAM_MULTIPLE: float = 2.5

# A compact deterministic distribution with the supplied corpus landmarks:
# mean 23.5, median 5, nearest-rank p90 32, and max 1,721 chunks/file.
_MEASURED_CHUNK_DISTRIBUTION: tuple[int, ...] = (
    *((5,) * 65),
    *((6,) * 49),
    32,
    32,
    54,
    54,
    *((55,) * 8),
    56,
    1721,
)


@dataclass(frozen=True, slots=True)
class SyntheticCorpusSpec:
    """Shape of the deterministic Markdown corpus used by the probe."""

    file_count: int = len(_MEASURED_CHUNK_DISTRIBUTION)
    chunks_per_file_distribution: tuple[int, ...] = _MEASURED_CHUNK_DISTRIBUTION
    chunk_chars: int = 1400
    seed: int = 34

    def __post_init__(self) -> None:
        if self.file_count < 1:
            raise ValueError("file_count must be at least 1")
        if not self.chunks_per_file_distribution or any(
            count < 1 for count in self.chunks_per_file_distribution
        ):
            raise ValueError("chunks_per_file_distribution must contain positive counts")
        if self.chunk_chars < 1:
            raise ValueError("chunk_chars must be at least 1")


_DEFAULT_SYNTHETIC_CORPUS_SPEC = SyntheticCorpusSpec()


@dataclass(frozen=True, slots=True)
class EmbedCallTiming:
    """The exact embed interval and SQL counter at the writer boundary."""

    entered_ns: int
    exited_ns: int
    inputs: int
    sql_ns_at_exit: int


@dataclass(frozen=True, slots=True)
class FileTiming:
    """One embedded file's complete four-timestamp decomposition."""

    path: str
    inputs: int
    t_producer_ns: int
    t_embed_ns: int
    t_writer_ns: int
    t_writer_sql_ns: int
    t_total_ns: int

    def __post_init__(self) -> None:
        if self.t_producer_ns + self.t_embed_ns + self.t_writer_ns != self.t_total_ns:
            raise ValueError(f"timing decomposition does not sum exactly for {self.path}")
        if self.t_writer_sql_ns > self.t_writer_ns:
            raise ValueError(f"writer SQL time exceeds writer time for {self.path}")


@dataclass(frozen=True, slots=True)
class PassTiming:
    """Aggregate timings for one complete pass over the generated corpus."""

    files: tuple[FileTiming, ...]
    inputs: int
    no_embed_files: int
    t_producer_ns: int
    t_embed_ns: int
    t_writer_ns: int
    t_writer_sql_ns: int
    t_total_ns: int

    def __post_init__(self) -> None:
        if self.t_producer_ns + self.t_embed_ns + self.t_writer_ns != self.t_total_ns:
            raise ValueError("aggregate timing decomposition does not sum exactly")
        if self.t_writer_sql_ns > self.t_writer_ns:
            raise ValueError("aggregate writer SQL time exceeds writer time")
        if self.inputs != sum(item.inputs for item in self.files):
            raise ValueError("aggregate input count disagrees with file timings")

    @property
    def producer_ms_per_input(self) -> float:
        return _per_input_ms(self.t_producer_ns, self.inputs)

    @property
    def writer_ms_per_input(self) -> float:
        return _per_input_ms(self.t_writer_ns, self.inputs)

    @property
    def writer_sql_ms_per_input(self) -> float:
        return _per_input_ms(self.t_writer_sql_ns, self.inputs)


@dataclass(frozen=True, slots=True)
class Decomposition:
    """Five-pass decomposition and its precommitted branch result."""

    passes: tuple[PassTiming, ...]
    p_ms: float
    w_ms: float
    n_ms: float
    baseline_ms: float
    upstream_multiple: float
    arch_samples: tuple[float, ...]
    s_lo: float
    s_hi: float
    branch: str
    offthread_comparison: float
    offthread_pipeline_ceiling: float
    producer_thread_headroom: float
    writer_sql_ms: float
    audit_ns_per_record: float

    def __post_init__(self) -> None:
        if len(self.passes) != CEILING_REPETITIONS:
            raise ValueError(f"decomposition requires K={CEILING_REPETITIONS} repetitions")
        if len(self.arch_samples) != len(self.passes):
            raise ValueError("ceiling sample count disagrees with pass count")
        if self.producer_thread_headroom < 0:
            raise ValueError("producer-thread headroom cannot be negative")


class _TimingConnection:
    """Connection proxy that times SQLite calls without changing their results."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.sql_ns = 0

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        started = time.perf_counter_ns()
        try:
            return self._conn.execute(sql, tuple(parameters))
        finally:
            self.sql_ns += time.perf_counter_ns() - started

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        started = time.perf_counter_ns()
        try:
            return self._conn.executemany(sql, parameters)
        finally:
            self.sql_ns += time.perf_counter_ns() - started

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class StubEmbedder:
    """Network-free embedder that records the existing core's stage boundary."""

    def __init__(
        self,
        *,
        sql_clock: Callable[[], int] | None = None,
        max_concurrency: int = 1,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.calls: list[EmbedCallTiming] = []
        self._sql_clock = sql_clock if sql_clock is not None else lambda: 0
        self._zero_vector = [0.0] * EMBED_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        entered = time.perf_counter_ns()
        vectors = [self._zero_vector] * len(texts)
        exited = time.perf_counter_ns()
        self.calls.append(
            EmbedCallTiming(
                entered_ns=entered,
                exited_ns=exited,
                inputs=len(texts),
                sql_ns_at_exit=self._sql_clock(),
            )
        )
        return vectors

    def probe(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise IndexError(message)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    return parser.parse_args(argv)


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _refuse_store_under_source_root(store: Path) -> None:
    resolved = store.expanduser().resolve()
    try:
        resolved.relative_to(_source_root())
    except ValueError:
        return
    raise IndexError(f"cost probe --store must be outside the palace source root: {resolved}")


def generate_corpus(root: Path, spec: SyntheticCorpusSpec) -> list[Path]:
    """Write deterministic Markdown files whose section counts follow ``spec``."""
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    distribution = spec.chunks_per_file_distribution
    for file_index in range(spec.file_count):
        chunk_count = distribution[file_index % len(distribution)]
        file_path = root / f"synthetic-{file_index:04d}.md"
        sections: list[str] = []
        for chunk_index in range(chunk_count):
            marker = f"seed={spec.seed} file={file_index} chunk={chunk_index} "
            body = (marker * ((spec.chunk_chars // len(marker)) + 1))[: spec.chunk_chars]
            sections.append(f"## Synthetic {chunk_index:04d}\n\n{body}\n")
        file_path.write_text("\n".join(sections), encoding="utf-8")
        paths.append(file_path)
    return paths


def measure_pass(store: Path, root: Path) -> PassTiming:
    """Measure one exact four-timestamp pass over ``root`` using a disk DB."""
    store.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(prepare_chunks_db_path(store))
    try:
        init_db(conn, store=store)
        identity = resolve_identity(store)
        timed = _TimingConnection(conn)
        embedder = StubEmbedder(sql_clock=lambda: timed.sql_ns)
        engine = IgnoreEngine(root)
        file_timings: list[FileTiming] = []
        no_embed_files = 0
        for relative, absolute in enumerate_root(root=root, engine=engine, resume_from=None):
            before_calls = len(embedder.calls)
            started = time.perf_counter_ns()
            index_one(
                conn=cast(sqlite3.Connection, timed),
                embedder=embedder,
                identity=identity,
                file_kind=classify_kind(absolute),
                change_kind="created",
                path=absolute,
                watch_root=root.resolve(),
                store=store,
                log=lambda _line: None,
            )
            finished = time.perf_counter_ns()
            if len(embedder.calls) == before_calls:
                no_embed_files += 1
                continue
            call = embedder.calls[-1]
            item = FileTiming(
                path=relative.as_posix(),
                inputs=call.inputs,
                t_producer_ns=call.entered_ns - started,
                t_embed_ns=call.exited_ns - call.entered_ns,
                t_writer_ns=finished - call.exited_ns,
                t_writer_sql_ns=timed.sql_ns - call.sql_ns_at_exit,
                t_total_ns=finished - started,
            )
            file_timings.append(item)
    finally:
        conn.close()

    return PassTiming(
        files=tuple(file_timings),
        inputs=sum(item.inputs for item in file_timings),
        no_embed_files=no_embed_files,
        t_producer_ns=sum(item.t_producer_ns for item in file_timings),
        t_embed_ns=sum(item.t_embed_ns for item in file_timings),
        t_writer_ns=sum(item.t_writer_ns for item in file_timings),
        t_writer_sql_ns=sum(item.t_writer_sql_ns for item in file_timings),
        t_total_ns=sum(item.t_total_ns for item in file_timings),
    )


def measure_audit(store: Path, records: Sequence[dict[str, Any]]) -> int:
    """Return total nanoseconds spent appending representative audit records."""
    started = time.perf_counter_ns()
    for record in records:
        append_record(store=store, record=record)
    return time.perf_counter_ns() - started


def ceiling_arch(b: float, local_ms: float, n: float, m: float) -> float:
    """Return the reported ceiling for palace's two-stage pipeline."""
    _require_positive(b=b, n=n, m=m)
    if local_ms < 0:
        raise ValueError("local_ms must be non-negative")
    return b / max(local_ms, n / m)


def ceiling_offthread_comparison(b: float, w: float, n: float, m: float) -> float:
    """Return the phase-table comparison, which is not palace's bound."""
    _require_positive(b=b, n=n, m=m)
    if w < 0:
        raise ValueError("w must be non-negative")
    denominator = w + n / m
    return math.inf if denominator == 0 else b / denominator


def reported_ceiling(b: float, p: float, w: float, n: float, m: float) -> float:
    """Return ``S_arch`` alone; the labelled comparison never participates."""
    return ceiling_arch(b, p + w, n, m)


def producer_thread_headroom(b: float, p: float, w: float, n: float, m: float) -> float:
    """Return non-negative headroom from the actual producer-off-thread pipeline."""
    _require_positive(b=b, n=n, m=m)
    if p < 0 or w < 0:
        raise ValueError("p and w must be non-negative")
    current = reported_ceiling(b, p, w, n, m)
    offthread = b / max(p, w, n / m)
    return offthread - current


def select_branch(s_lo: float, s_hi: float) -> str:
    """Apply the precommitted exhaustive A/B/C rule to one interval."""
    if s_lo > s_hi:
        raise ValueError("S_lo cannot exceed S_hi")
    if s_lo >= BRANCH_THRESHOLD:
        return "A"
    if s_hi < BRANCH_THRESHOLD:
        return "B"
    return "C"


def measure_decomposition(
    store: Path,
    *,
    spec: SyntheticCorpusSpec | None = None,
) -> Decomposition:
    """Run K complete disk-backed passes and apply the precommitted branch rule."""
    resolved_spec = spec if spec is not None else _DEFAULT_SYNTHETIC_CORPUS_SPEC
    store = store.expanduser().resolve()
    _refuse_store_under_source_root(store)
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="palace-costprobe-", dir=store) as temp_name:
        temp_root = Path(temp_name)
        corpus_root = temp_root / "corpus"
        generate_corpus(corpus_root, resolved_spec)
        passes = tuple(
            measure_pass(temp_root / f"store-{index}", corpus_root)
            for index in range(CEILING_REPETITIONS)
        )
        audit_records = tuple(
            build_record(
                provider="openrouter",
                upstream_configured="DeepInfra",
                upstream_observed="DeepInfra",
                model="Qwen/Qwen3-Embedding-8B",
                dim=EMBED_DIM,
                texts=(f"synthetic audit {index}",),
                prompt_tokens=4,
                cost_usd=0.0,
                http_status=200,
                latency_ms=1,
            )
            for index in range(100)
        )
        audit_ns = measure_audit(temp_root / "audit-store", audit_records)

    p_samples = tuple(item.producer_ms_per_input for item in passes)
    w_samples = tuple(item.writer_ms_per_input for item in passes)
    arch_samples = tuple(
        reported_ceiling(
            p + w + RECORDED_NETWORK_MS_PER_INPUT,
            p,
            w,
            RECORDED_NETWORK_MS_PER_INPUT,
            MEASURED_UPSTREAM_MULTIPLE,
        )
        for p, w in zip(p_samples, w_samples, strict=True)
    )
    p_ms = statistics.median(p_samples)
    w_ms = statistics.median(w_samples)
    baseline = p_ms + w_ms + RECORDED_NETWORK_MS_PER_INPUT
    offthread_comparison = ceiling_offthread_comparison(
        baseline, w_ms, RECORDED_NETWORK_MS_PER_INPUT, MEASURED_UPSTREAM_MULTIPLE
    )
    actual_offthread = baseline / max(
        p_ms,
        w_ms,
        RECORDED_NETWORK_MS_PER_INPUT / MEASURED_UPSTREAM_MULTIPLE,
    )
    s_lo = min(arch_samples)
    s_hi = max(arch_samples)
    return Decomposition(
        passes=passes,
        p_ms=p_ms,
        w_ms=w_ms,
        n_ms=RECORDED_NETWORK_MS_PER_INPUT,
        baseline_ms=baseline,
        upstream_multiple=MEASURED_UPSTREAM_MULTIPLE,
        arch_samples=arch_samples,
        s_lo=s_lo,
        s_hi=s_hi,
        branch=select_branch(s_lo, s_hi),
        offthread_comparison=offthread_comparison,
        offthread_pipeline_ceiling=actual_offthread,
        producer_thread_headroom=producer_thread_headroom(
            baseline,
            p_ms,
            w_ms,
            RECORDED_NETWORK_MS_PER_INPUT,
            MEASURED_UPSTREAM_MULTIPLE,
        ),
        writer_sql_ms=statistics.median(item.writer_sql_ms_per_input for item in passes),
        audit_ns_per_record=audit_ns / len(audit_records),
    )


def render_markdown(decomposition: Decomposition) -> str:
    """Render the load-bearing measurements with topology labels attached."""
    samples = ", ".join(f"{value:.3f}" for value in decomposition.arch_samples)
    lines = [
        "# Palace index local-cost decomposition",
        "",
        f"- K: {len(decomposition.passes)}",
        "- per-pass inputs: " + ", ".join(str(item.inputs) for item in decomposition.passes),
        "- per-pass files excluded because they produced no embed inputs: "
        + ", ".join(str(item.no_embed_files) for item in decomposition.passes),
        f"- p (producer): {decomposition.p_ms:.6f} ms/input",
        f"- w (writer): {decomposition.w_ms:.6f} ms/input",
        f"- n (recorded network): {decomposition.n_ms:.3f} ms/input",
        f"- B (serial baseline): {decomposition.baseline_ms:.6f} ms/input",
        f"- upstream multiple m: {decomposition.upstream_multiple:.3f}",
        f"- S_arch samples: [{samples}]",
        f"- S_arch interval: [{decomposition.s_lo:.3f}, {decomposition.s_hi:.3f}]",
        f"- branch: {decomposition.branch}",
        "- scale limitation: each repetition used a fresh store that never exceeded "
        "about 3,008 chunks; writer time is therefore a lower bound for the roughly "
        "160,000-row target corpus, biasing the branch toward A. The measured local "
        "stage would need to grow about 57x to cross the L = n decision boundary.",
        f"- writer SQL: {decomposition.writer_sql_ms:.6f} ms/input",
        f"- audit append: {decomposition.audit_ns_per_record:.0f} ns/record",
        "",
        "## Producer-off-thread labelled comparison",
        "",
        "`S_table = B / (w + n/m)` is retained for continuity with the phase table; "
        "it is not a bound on this architecture.",
        "",
        f"- S_table: {decomposition.offthread_comparison:.3f}",
        f"- actual off-thread pipeline ceiling: {decomposition.offthread_pipeline_ceiling:.3f}",
        f"- producer-thread headroom: {decomposition.producer_thread_headroom:.3f}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        decomposition = measure_decomposition(args.store)
        print(render_markdown(decomposition), end="")
        return 0
    except (IndexError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def _per_input_ms(nanoseconds: int, inputs: int) -> float:
    if inputs < 1:
        raise ValueError("a measured pass produced no embed inputs")
    return nanoseconds / inputs / 1_000_000


def _require_positive(*, b: float, n: float, m: float) -> None:
    if b <= 0 or n <= 0 or m <= 0:
        raise ValueError("b, n, and m must be positive")


if __name__ == "__main__":
    raise SystemExit(main())
