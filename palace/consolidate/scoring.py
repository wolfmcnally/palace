"""The six-signal OpenClaw Deep Sleep scoring + three-gate promotion check.

Each candidate is scored on six signals in [0, 1] — relevance, frequency,
recency, importance, confidence, novelty — combined into a weighted total via
:data:`palace.consolidate.config.WEIGHTS`. The signals split between LLM-derived
(relevance / importance / confidence, and the model's novelty view) and pure
functions of the candidate + run state (recency from event_time, frequency
from source count, novelty from embedding similarity against existing facts).

The three binding promotion gates (``policies/fact-schema.md`` §"Gates for
promotion") are applied AFTER scoring: a candidate promotes only when its
relevance, confidence, and corroboration all pass. Crucially the corroboration
gate reads the candidate's source COUNT (or its authored flag), not the
frequency *weight* — the weight shapes the ranking, the count is the gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from palace.consolidate.config import (
    FREQUENCY_SATURATION_COUNT,
    GATE_CONFIDENCE,
    GATE_CORROBORATION,
    GATE_RELEVANCE,
    RECENCY_HALFLIFE_DAYS,
    WEIGHTS,
)
from palace.consolidate.extract import Candidate
from palace.consolidate.llm import ConsolidatorLLM
from palace.index.embedder import Embedder

__all__ = [
    "SignalBreakdown",
    "frequency_score",
    "gate_verdict",
    "novelty_score",
    "recency_score",
    "score_candidate",
    "weighted_total",
]


@dataclass(frozen=True)
class SignalBreakdown:
    """The full scored audit of one candidate: six signals, total, gates, verdict."""

    candidate: Candidate
    relevance: float
    frequency: float
    recency: float
    importance: float
    confidence: float
    novelty: float
    weighted_total: float
    gate_relevance_pass: bool
    gate_confidence_pass: bool
    gate_corroboration_pass: bool
    verdict: str
    failed_gates: list[str]


def recency_score(event_time: str, run_day: date) -> float:
    """Exponential-decay recency: 1.0 same-day, 0.5 at the half-life, in [0, 1].

    Uses the ISO date prefix of ``event_time`` against ``run_day``. A missing
    or unparseable date scores 0.0 (a candidate with no event time is treated
    as maximally stale, not maximally fresh).
    """
    prefix = event_time[:10]
    try:
        when = date.fromisoformat(prefix)
    except ValueError:
        return 0.0
    age_days = (run_day - when).days
    if age_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / RECENCY_HALFLIFE_DAYS))


def frequency_score(source_count: int) -> float:
    """Linear-to-saturation frequency: 0 at zero sources, 1.0 at saturation."""
    if source_count <= 0:
        return 0.0
    return min(1.0, source_count / FREQUENCY_SATURATION_COUNT)


def novelty_score(cand_vec: list[float], existing_vecs: list[list[float]]) -> float:
    """Novelty = ``1 - max_cosine`` against existing fact vectors.

    Returns 1.0 when there are no existing facts (everything is novel). A
    candidate that restates a known fact has a near-1.0 max cosine and so a
    near-0.0 novelty.
    """
    if not existing_vecs:
        return 1.0
    best = max(_cosine(cand_vec, vec) for vec in existing_vecs)
    novelty = 1.0 - best
    if novelty < 0.0:
        return 0.0
    if novelty > 1.0:
        return 1.0
    return novelty


def weighted_total(
    *,
    relevance: float,
    frequency: float,
    recency: float,
    importance: float,
    confidence: float,
    novelty: float,
) -> float:
    """Combine the six signals via the configured weights."""
    return (
        WEIGHTS["relevance"] * relevance
        + WEIGHTS["frequency"] * frequency
        + WEIGHTS["recency"] * recency
        + WEIGHTS["importance"] * importance
        + WEIGHTS["confidence"] * confidence
        + WEIGHTS["novelty"] * novelty
    )


def gate_verdict(
    candidate: Candidate, *, relevance: float, confidence: float
) -> tuple[bool, bool, bool, list[str]]:
    """Apply the three binding gates; return per-gate bools + failed-gate names.

    Corroboration passes when the candidate has ≥ ``GATE_CORROBORATION``
    distinct source events OR one explicit authored source — the count gate of
    ``policies/fact-schema.md`` §Gates, NOT the frequency weight. The
    "authored?" predicate is source-kind-aware (sessions: Wolf-authored turn;
    captures: ``user:*`` source).
    """
    relevance_pass = relevance > GATE_RELEVANCE
    confidence_pass = confidence > GATE_CONFIDENCE
    corroboration_pass = len(candidate.source_ids) >= GATE_CORROBORATION or candidate.authored
    failed: list[str] = []
    if not relevance_pass:
        failed.append("relevance")
    if not confidence_pass:
        failed.append("confidence")
    if not corroboration_pass:
        failed.append("corroboration")
    return relevance_pass, confidence_pass, corroboration_pass, failed


def score_candidate(
    candidate: Candidate,
    *,
    llm: ConsolidatorLLM,
    embedder: Embedder,
    existing_claims: list[str],
    existing_vecs: list[list[float]],
    run_day: date,
) -> SignalBreakdown:
    """Score one candidate on all six signals and apply the three gates.

    LLM-derived: relevance, importance, confidence (and the model's novelty
    view, surfaced in the audit but not the authoritative novelty). Pure:
    recency (event_time vs run_day), frequency (source count), novelty
    (embedding cosine vs ``existing_vecs``).
    """
    signals = llm.score_signals(candidate, existing_facts=existing_claims)
    cand_vec = embedder.embed([candidate.claim])[0]

    relevance = signals.relevance
    importance = signals.importance
    confidence = signals.confidence
    recency = recency_score(candidate.event_time, run_day)
    frequency = frequency_score(len(candidate.source_ids))
    novelty = novelty_score(cand_vec, existing_vecs)

    total = weighted_total(
        relevance=relevance,
        frequency=frequency,
        recency=recency,
        importance=importance,
        confidence=confidence,
        novelty=novelty,
    )

    rel_pass, conf_pass, corr_pass, failed = gate_verdict(
        candidate, relevance=relevance, confidence=confidence
    )
    verdict = "PROMOTE" if not failed else "DISCARD"

    return SignalBreakdown(
        candidate=candidate,
        relevance=relevance,
        frequency=frequency,
        recency=recency,
        importance=importance,
        confidence=confidence,
        novelty=novelty,
        weighted_total=total,
        gate_relevance_pass=rel_pass,
        gate_confidence_pass=conf_pass,
        gate_corroboration_pass=corr_pass,
        verdict=verdict,
        failed_gates=failed,
    )


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 on a zero vector."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
