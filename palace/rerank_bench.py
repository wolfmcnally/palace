"""Benchmark the complete warm rerank boundary against a question set."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from palace.index._errors import IndexError
from palace.index.build import build
from palace.index.config import chunks_db_path
from palace.index.embedder import Embedder, OllamaEmbedder
from palace.rerank import (
    Reranker,
    RerankStatus,
    RerankUnavailableError,
    load_reranker,
    rerank,
)
from palace.retrieval_config import (
    RERANK_IN,
    RERANK_LATENCY_BUDGET_MS,
    RERANK_MAX_TOKENS,
    RERANK_OUT,
    rerank_model_dir,
)
from palace.search import SearchHit, rerank_candidates, search, search_reranked
from palace.search import fts5_query as _search_fts5_query

UNATTRIBUTED_LABEL: str = "UNATTRIBUTED — stub question set; Phase 8 supplies the real one"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_QUESTIONS = _REPO_ROOT / "tests" / "fixtures" / "rerank-bench" / "questions.jsonl"
_DEFAULT_CORPUS = _REPO_ROOT / "tests" / "fixtures" / "rerank-bench" / "corpus"

__all__ = [
    "BenchQuestion",
    "QuestionResult",
    "UNATTRIBUTED_LABEL",
    "load_questions",
    "main",
    "render_report",
]


@dataclass(frozen=True)
class BenchQuestion:
    id: str
    question: str
    expected_facts: list[str]
    expected_sources: list[str]
    tags: list[str]
    added: str


@dataclass(frozen=True)
class QuestionResult:
    question: BenchQuestion
    off_chunk_ids: list[str]
    on_chunk_ids: list[str]
    on_status: RerankStatus
    off_expected_rank: int | None
    on_expected_rank: int | None
    rerank_ms_samples: list[float]
    off_wall_ms: float
    on_wall_ms: float
    top1_changed: bool


def load_questions(path: Path) -> list[BenchQuestion]:
    """Load strict JSONL questions, rejecting missing, malformed, or empty input."""
    if not path.is_file():
        raise IndexError(f"question set not found: {path}")
    questions: list[BenchQuestion] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("line is not a JSON object")
            questions.append(
                BenchQuestion(
                    id=str(value["id"]),
                    question=str(value["question"]),
                    expected_facts=_string_list(value["expected_facts"]),
                    expected_sources=_string_list(value["expected_sources"]),
                    tags=_string_list(value["tags"]),
                    added=str(value["added"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IndexError(f"invalid question set {path} line {line_number}: {exc}") from exc
    if not questions:
        raise IndexError(f"question set is empty: {path}")
    return questions


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("expected an array of strings")
    return list(value)


def fts5_query(question: str) -> str:
    """Render a natural-language question as a safe FTS5 MATCH expression.

    Thin alias for :func:`palace.search.fts5_query`, which is now the single
    implementation: ``_bm25_search`` renders every query through it, so the
    bench and the CLI cannot drift apart on what a question means. Retained as
    a named bench surface because the bench renders *before* calling
    :func:`palace.search.search`, keeping the recorded per-question expression
    identical to the one the measurement was taken with. The rendering is
    idempotent, so the second pass inside ``_bm25_search`` is a no-op.
    """
    return _search_fts5_query(question)


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over a sorted copy."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= q <= 1.0:
        raise ValueError("percentile q must be in [0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _expected_rank(hits: Sequence[SearchHit], expected_sources: Sequence[str]) -> int | None:
    expected_names = {Path(source).name for source in expected_sources}
    for rank, hit in enumerate(hits, start=1):
        if Path(hit.path).name in expected_names:
            return rank
    return None


def render_report(
    *,
    results: Sequence[QuestionResult],
    store: Path,
    model_dir: Path,
    rerank_in: int,
    limit: int,
    repeats: int,
    model_load_ms: float,
) -> str:
    """Render an explicitly unattributed, provenance-bearing benchmark report."""
    rerank_samples = [sample for result in results for sample in result.rerank_ms_samples]
    off_samples = [result.off_wall_ms for result in results]
    on_samples = [result.on_wall_ms for result in results]
    p50 = _percentile(rerank_samples, 0.50)
    p95 = _percentile(rerank_samples, 0.95)
    changed = sum(result.top1_changed for result in results)
    movements = [
        float(result.off_expected_rank - result.on_expected_rank)
        for result in results
        if result.off_expected_rank is not None and result.on_expected_rank is not None
    ]
    mean_movement = sum(movements) / len(movements) if movements else 0.0

    lines = [
        "palace rerank benchmark",
        f"store: {store}",
        f"model_dir: {model_dir}",
        f"RERANK_MAX_TOKENS = {RERANK_MAX_TOKENS}",
        f"rerank_in = {rerank_in}; limit = {limit}; repeats = {repeats}",
        (
            "rerank_ms boundary: complete warm rerank() — candidate_text + tokenize + "
            "session.run + sort; artifact acquisition/verification + ONNX construction + "
            "loader probe warm-up are included in model_load_ms"
        ),
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"question {result.question.id}: {result.question.question}",
                f"  off: {','.join(result.off_chunk_ids)}",
                f"  on:  {','.join(result.on_chunk_ids)} (status={result.on_status})",
                (
                    f"  expected_rank: off={result.off_expected_rank} "
                    f"on={result.on_expected_rank}; top1_changed={result.top1_changed}"
                ),
            ]
        )
    lines.extend(
        [
            "",
            (
                f"{UNATTRIBUTED_LABEL} — rerank latency: p50={p50:.3f} ms; "
                f"p95={p95:.3f} ms; model_load_ms={model_load_ms:.3f}"
            ),
            (
                f"{UNATTRIBUTED_LABEL} — end-to-end wall clock: "
                f"off_p50={_percentile(off_samples, 0.50):.3f} ms; "
                f"on_p50={_percentile(on_samples, 0.50):.3f} ms"
            ),
            (
                f"{UNATTRIBUTED_LABEL} — ordering: top1_changed={changed}/{len(results)}; "
                f"mean_expected_rank_movement={mean_movement:.3f}"
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _run_questions(
    *,
    questions: Sequence[BenchQuestion],
    store: Path,
    rerank_in: int,
    limit: int,
    repeats: int,
    embedder: Embedder,
    reranker_instance: Reranker,
) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    for question in questions:
        _fused_pool, candidates = rerank_candidates(
            query=fts5_query(question.question),
            store=store,
            rerank_in=rerank_in,
            embedder=embedder,
        )
        samples: list[float] = []
        for _repeat in range(repeats):
            started = time.perf_counter_ns()
            rerank(
                query=fts5_query(question.question),
                candidates=candidates,
                top_k=limit,
                reranker=reranker_instance,
            )
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)

        started = time.perf_counter_ns()
        off_hits = search(
            query=fts5_query(question.question),
            store=store,
            limit=limit,
            embedder=embedder,
        )
        off_wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0

        started = time.perf_counter_ns()
        on_result = search_reranked(
            query=fts5_query(question.question),
            store=store,
            rerank_in=rerank_in,
            limit=limit,
            embedder=embedder,
            reranker=reranker_instance,
        )
        on_hits = on_result.hits
        on_wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        results.append(
            QuestionResult(
                question=question,
                off_chunk_ids=[hit.chunk_id for hit in off_hits],
                on_chunk_ids=[hit.chunk_id for hit in on_hits],
                on_status=on_result.status,
                off_expected_rank=_expected_rank(off_hits, question.expected_sources),
                on_expected_rank=_expected_rank(on_hits, question.expected_sources),
                rerank_ms_samples=samples,
                off_wall_ms=off_wall_ms,
                on_wall_ms=on_wall_ms,
                top1_changed=bool(
                    off_hits and on_hits and off_hits[0].chunk_id != on_hits[0].chunk_id
                ),
            )
        )
    return results


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--questions", type=Path, default=_DEFAULT_QUESTIONS)
    parser.add_argument("--rerank-in", type=_positive_int, default=RERANK_IN)
    parser.add_argument("--limit", type=_positive_int, default=RERANK_OUT)
    parser.add_argument("--repeats", type=_positive_int, default=3)
    parser.add_argument(
        "--assert-under-ms",
        type=float,
        default=float(RERANK_LATENCY_BUDGET_MS),
    )
    parser.add_argument("--keep-store", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    temporary_store = args.store is None
    if temporary_store:
        resolved_store = Path(tempfile.mkdtemp(prefix="palace-rerank-bench.")).resolve()
    else:
        resolved_store = args.store.expanduser().resolve()
    resolved_model_dir = rerank_model_dir().resolve()

    reranker_instance: Reranker | None = None
    embedder: OllamaEmbedder | None = None
    try:
        questions = load_questions(args.questions)
        if args.store is not None:
            if not chunks_db_path(resolved_store).is_file():
                raise IndexError(
                    f"chunks DB not found under --store {resolved_store}; "
                    "build it before benchmarking"
                )
        else:
            # One embedder for the hermetic build and the query path, probed
            # before the build so an unreachable Ollama names its own fix up
            # front instead of surfacing from inside build(), and closed by the
            # same finally that owns the query-path client.
            embedder = OllamaEmbedder()
            embedder.probe()
            build(
                store=resolved_store,
                watch_root_filter=_DEFAULT_CORPUS,
                embedder=embedder,
            )

        model_started = time.perf_counter_ns()
        try:
            reranker_instance = load_reranker()
        except RerankUnavailableError as exc:
            raise IndexError(
                f"reranker unavailable at {resolved_model_dir}: {exc}; "
                "run ./bin/palace-rerank-model"
            ) from exc
        model_load_ms = (time.perf_counter_ns() - model_started) / 1_000_000.0

        if embedder is None:
            embedder = OllamaEmbedder()
            embedder.probe()
        results = _run_questions(
            questions=questions,
            store=resolved_store,
            rerank_in=args.rerank_in,
            limit=args.limit,
            repeats=args.repeats,
            embedder=embedder,
            reranker_instance=reranker_instance,
        )
        report = render_report(
            results=results,
            store=resolved_store,
            model_dir=resolved_model_dir,
            rerank_in=args.rerank_in,
            limit=args.limit,
            repeats=args.repeats,
            model_load_ms=model_load_ms,
        )
        print(report, end="", flush=True)
        p50 = _percentile(
            [sample for result in results for sample in result.rerank_ms_samples],
            0.50,
        )
        if p50 > args.assert_under_ms:
            print(
                f"error: rerank p50 {p50:.3f} ms exceeds --assert-under-ms "
                f"{args.assert_under_ms:.3f}",
                file=sys.stderr,
                flush=True,
            )
            return 1
        return 0
    except (IndexError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if embedder is not None:
            embedder.close()
        if reranker_instance is not None:
            reranker_instance.close()
        if temporary_store and not args.keep_store:
            shutil.rmtree(resolved_store, ignore_errors=True)
        elif temporary_store and args.keep_store:
            print(f"rerank-bench: kept temporary store at {resolved_store}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
