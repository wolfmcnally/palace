"""Local cross-encoder reranking over hydrated search candidates."""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from palace.index._errors import IndexError
from palace.index.enrich import scored_text
from palace.rerank_model import (
    RERANK_MODEL_FILENAME,
    RERANK_TOKENIZER_FILENAME,
    ModelProvisionError,
    provision_rerank_model,
)
from palace.retrieval_config import RERANK_MAX_TOKENS, rerank_model_dir

__all__ = [
    "CrossEncoderReranker",
    "RERANK_MODEL_FILENAME",
    "RERANK_TOKENIZER_FILENAME",
    "RerankCandidate",
    "RerankStatus",
    "RerankUnavailableError",
    "RerankedCandidate",
    "Reranker",
    "candidate_text",
    "load_reranker",
    "rerank",
]


RerankStatus = Literal["applied", "disabled", "failed"]


class RerankUnavailableError(IndexError):
    """An operational reranker failure for which fused results remain usable."""


@dataclass(frozen=True)
class RerankCandidate:
    """One chunk hydrated for scoring and Phase 7.2's breadcrumb seam.

    ``path`` remains watch-root-relative. The path, watch root, heading, and
    kind are carried now so contextual enrichment does not have to re-plumb
    the hydration query later.
    """

    chunk_id: str
    path: str
    watch_root: str
    heading: str | None
    kind: str
    body: str


@dataclass(frozen=True)
class RerankedCandidate:
    """A candidate plus its raw cross-encoder logit and 1-based rank."""

    candidate: RerankCandidate
    score: float | None
    rank: int


class Reranker(Protocol):
    """The scoring contract used by the retrieval composition layer."""

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...

    def probe(self) -> None: ...

    def close(self) -> None: ...


def candidate_text(candidate: RerankCandidate) -> str:
    """Return shared enriched text; ``watch_root`` is deliberately unused."""
    return scored_text(path=candidate.path, heading=candidate.heading, body=candidate.body)


def _encode_pairs(
    tokenizer: Any,
    measure_tokenizer: Any,
    query: str,
    texts: Sequence[str],
    *,
    max_tokens: int,
    pair_special: int,
) -> dict[str, list[list[int]]]:
    """Pair-encode ``query`` and passages with an explicit query-budget guard."""
    query_len = len(measure_tokenizer.encode(query, add_special_tokens=False).ids)
    if query_len + pair_special + 1 > max_tokens:
        raise IndexError(
            f"query is {query_len} tokens; the reranker pair budget is {max_tokens} tokens "
            "(RERANK_MAX_TOKENS) — rerun with --no-rerank for fused results, shorten the "
            "query, or raise the cap in palace/retrieval_config.py"
        )
    if not texts:
        return {"input_ids": [], "attention_mask": [], "token_type_ids": []}

    encodings = tokenizer.encode_batch([(query, text) for text in texts])
    return {
        "input_ids": [encoding.ids for encoding in encodings],
        "attention_mask": [encoding.attention_mask for encoding in encodings],
        "token_type_ids": [encoding.type_ids for encoding in encodings],
    }


def _read_scores(outputs: Sequence[Any]) -> list[float]:
    """Read raw relevance logits from a ``[batch, 1]`` or ``[batch]`` output."""
    if not outputs:
        raise RerankUnavailableError("reranker returned no outputs")
    logits = outputs[0]
    shape = tuple(getattr(logits, "shape", ()))
    if len(shape) == 2 and shape[1] == 1:
        scores = [float(row[0]) for row in logits]
    elif len(shape) == 1:
        scores = [float(value) for value in logits]
    else:
        raise RerankUnavailableError(f"reranker logits have unexpected shape {shape}")
    for index, score in enumerate(scores):
        if not math.isfinite(score):
            raise RerankUnavailableError(
                f"reranker returned non-finite logit at index {index}: {score}"
            )
    return scores


def rerank(
    *,
    query: str,
    candidates: Sequence[RerankCandidate],
    top_k: int,
    reranker: Reranker | None = None,
) -> list[RerankedCandidate]:
    """Score, deterministically order, and truncate hydrated candidates."""
    if top_k <= 0:
        raise IndexError("top_k must be positive")
    if reranker is None:
        return [
            RerankedCandidate(candidate=candidate, score=None, rank=rank)
            for rank, candidate in enumerate(candidates[:top_k], start=1)
        ]
    if not candidates:
        return []

    scores = reranker.score(query, [candidate_text(candidate) for candidate in candidates])
    if len(scores) != len(candidates):
        raise RerankUnavailableError(
            f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
        )
    scored = list(zip(candidates, scores, strict=True))
    scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
    return [
        RerankedCandidate(candidate=candidate, score=float(score), rank=rank)
        for rank, (candidate, score) in enumerate(scored[:top_k], start=1)
    ]


class CrossEncoderReranker:
    """ONNX ``ms-marco-MiniLM-L-6-v2`` scorer on the CPU provider."""

    def __init__(self, *, model_dir: Path, max_tokens: int = RERANK_MAX_TOKENS) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer_text = (model_dir / RERANK_TOKENIZER_FILENAME).read_text(encoding="utf-8")
        self._tokenizer = Tokenizer.from_str(tokenizer_text)
        self._measure = Tokenizer.from_str(tokenizer_text)
        self._max_tokens = max_tokens
        self._pair_special = len(self._measure.encode("", "").ids)

        pad_id = self._tokenizer.token_to_id("[PAD]")
        if pad_id is None:
            raise IndexError("reranker tokenizer has no [PAD] token")
        self._tokenizer.enable_truncation(
            max_length=max_tokens,
            strategy="only_second",
            direction="right",
        )
        self._tokenizer.enable_padding(
            direction="right",
            pad_id=pad_id,
            pad_token="[PAD]",
        )

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session: Any | None = ort.InferenceSession(
            str(model_dir / RERANK_MODEL_FILENAME),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {value.name for value in self._session.get_inputs()}
        self._np = np

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Score all query/passage pairs in one ONNX batch."""
        if not texts:
            return []
        if self._session is None:
            raise IndexError("reranker is closed")
        try:
            encoded = _encode_pairs(
                self._tokenizer,
                self._measure,
                query,
                texts,
                max_tokens=self._max_tokens,
                pair_special=self._pair_special,
            )
        except IndexError:
            raise
        except Exception as exc:
            raise RerankUnavailableError(f"reranker tokenization failed: {exc}") from exc

        try:
            feed = {
                name: self._np.asarray(encoded[name], dtype=self._np.int64)
                for name in self._input_names
                if name in encoded
            }
            missing = self._input_names - feed.keys()
            if missing:
                raise RerankUnavailableError(
                    f"reranker graph declares unsupported inputs: {sorted(missing)}"
                )
            return _read_scores(self._session.run(None, feed))
        except RerankUnavailableError:
            raise
        except Exception as exc:
            raise RerankUnavailableError(f"reranker inference failed: {exc}") from exc

    def probe(self) -> None:
        """Warm the session and prove one pair scores successfully."""
        self.score("palace probe", ["palace probe passage"])

    def close(self) -> None:
        """Release the ONNX session; safe to call more than once."""
        self._session = None


_RERANK_WARNING_EMITTED: bool = False


def _warn_rerank_failed(*, detail: str, private: bool = False) -> None:
    global _RERANK_WARNING_EMITTED
    if _RERANK_WARNING_EMITTED:
        return
    _RERANK_WARNING_EMITTED = True
    remedy = "check the configured private endpoint" if private else "run ./bin/palace-rerank-model"
    print(
        f"palace search: rerank failed: {detail} — returning fused order ({remedy})",
        file=sys.stderr,
        flush=True,
    )


def _notice_model_fetch(model_dir: Path) -> None:
    print(
        f"palace search: fetching reranker model into {model_dir}",
        file=sys.stderr,
        flush=True,
    )


def load_reranker() -> Reranker:
    """Provision, load, and warm the shared model or raise a typed failure."""
    resolved_dir = rerank_model_dir()

    instance: CrossEncoderReranker | None = None
    try:
        provision_rerank_model(on_fetch=_notice_model_fetch)
    except ModelProvisionError as exc:
        raise RerankUnavailableError(str(exc)) from exc

    try:
        instance = CrossEncoderReranker(model_dir=resolved_dir)
        instance.probe()
        return instance
    except Exception as exc:
        if instance is not None:
            instance.close()
        if isinstance(exc, RerankUnavailableError):
            raise
        raise RerankUnavailableError(f"reranker initialization failed: {exc}") from exc
