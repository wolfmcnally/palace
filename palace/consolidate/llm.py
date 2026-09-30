"""The LLM determinism seam for the consolidator.

The consolidator's three LLM-driven steps — candidate extraction, signal
scoring, and contradiction detection — are isolated behind the
:class:`ConsolidatorLLM` Protocol so the pipeline is a pure function of
``(llm, embedder, files, clock)``. The production implementation
(:class:`OllamaConsolidatorLLM`) POSTs to a local Ollama daemon's
``/api/chat`` endpoint with ``options={"temperature": 0, "seed": 0}`` and a
JSON-schema ``format`` for structured outputs; tests inject a fake
implementation (``tests/consolidate/conftest.py``) so the suite never touches
a live model.

The HTTP client mirrors :class:`palace.index.embedder.OllamaEmbedder`: one
long-lived :class:`httpx.Client` per instance, an injectable ``transport=``
so unit tests use :class:`httpx.MockTransport`, and failure translation into a
single-line :class:`ConsolidationError`. The startup :meth:`probe` names the
operator fix (``ollama pull <EXTRACTION_MODEL>``) on a 404 / model-not-found
body, reading the model tag from :mod:`palace.consolidate.config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.config import EXTRACTION_MODEL, OLLAMA_BASE_URL

__all__ = [
    "ClaimLike",
    "ConsolidatorLLM",
    "ContradictionVerdict",
    "ExtractionContext",
    "OllamaConsolidatorLLM",
    "RawCandidate",
    "SignalScores",
]


# A short literal used for the startup probe — distinctive enough that a
# stray response is obviously synthetic.
_PROBE_PROMPT: str = "__palace_consolidate_probe__"


@dataclass(frozen=True)
class ExtractionContext:
    """Carried context for one extraction call.

    ``harness`` and ``authored`` come from the source unit; ``day`` is the run
    day's ISO string. The LLM uses them only as prompt context — the pipeline
    owns the authoritative provenance and authored flags.
    """

    harness: str | None
    authored: bool
    day: str


class ClaimLike(Protocol):
    """Anything carrying a ``claim`` string — both candidate shapes qualify.

    ``score_signals`` reads only the claim, so it accepts either a
    :class:`RawCandidate` or a :class:`palace.consolidate.extract.Candidate`
    without coupling the two modules.
    """

    @property
    def claim(self) -> str: ...


@dataclass(frozen=True)
class RawCandidate:
    """One candidate factual claim the extractor proposes from a source unit.

    ``summary`` is a short heading; ``claim`` is the prose fact body; ``tags``
    / ``refs`` are optional categorization / association strings the extractor
    suggests. The pipeline attaches the authoritative provenance and times.
    """

    summary: str
    claim: str
    tags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SignalScores:
    """The four LLM-derived signal floats in [0, 1] for one candidate.

    ``novelty_llm`` is the model's own novelty estimate; the pipeline's
    authoritative novelty signal comes from embedding similarity, but the
    scorer still surfaces the model's view for the audit. The recency and
    frequency signals are computed by pure helpers, not the LLM.
    """

    importance: float
    confidence: float
    novelty_llm: float
    relevance: float


@dataclass(frozen=True)
class ContradictionVerdict:
    """The model's adjudication of whether a candidate contradicts a fact."""

    contradicts: bool
    rationale: str
    recommended_resolution: str


class ConsolidatorLLM(Protocol):
    """The LLM contract the consolidation pipeline depends on.

    Implementations must be deterministic for a fixed input (the production
    one pins ``temperature=0, seed=0``) so a dry-run and a real run over the
    same fixtures produce identical breakdowns and verdicts.
    """

    def extract_candidates(
        self, session_text: str, *, context: ExtractionContext
    ) -> list[RawCandidate]:
        """Extract candidate factual claims from one source unit's text."""
        ...

    def score_signals(self, candidate: ClaimLike, *, existing_facts: list[str]) -> SignalScores:
        """Return the four LLM-derived signal scores for ``candidate``."""
        ...

    def detect_contradiction(
        self, candidate_claim: str, existing_claim: str
    ) -> ContradictionVerdict:
        """Adjudicate whether ``candidate_claim`` contradicts ``existing_claim``."""
        ...

    def probe(self) -> None:
        """Verify reachability + model availability; raise on failure."""
        ...

    def close(self) -> None:
        """Release any held resources (e.g., the HTTP client)."""
        ...


# JSON schemas Ollama's ``format`` field accepts for structured outputs. Each
# pins the exact shape the parser expects so the model cannot return prose.
_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "claim": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "claim"],
            },
        }
    },
    "required": ["candidates"],
}

_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "importance": {"type": "number"},
        "confidence": {"type": "number"},
        "novelty": {"type": "number"},
        "relevance": {"type": "number"},
    },
    "required": ["importance", "confidence", "novelty", "relevance"],
}

_CONTRADICTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contradicts": {"type": "boolean"},
        "rationale": {"type": "string"},
        "recommended_resolution": {"type": "string"},
    },
    "required": ["contradicts", "rationale", "recommended_resolution"],
}


class OllamaConsolidatorLLM:
    """Production :class:`ConsolidatorLLM` backed by a local Ollama daemon.

    Single long-lived :class:`httpx.Client` per instance. Every method POSTs
    to ``{base}/api/chat`` with the chosen model, ``options`` pinning a
    deterministic decode, and a JSON-schema ``format`` for structured output.
    """

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = EXTRACTION_MODEL,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ---------------------------------------------------------------- API

    def extract_candidates(
        self, session_text: str, *, context: ExtractionContext
    ) -> list[RawCandidate]:
        """Extract candidate claims from ``session_text`` via a chat call."""
        system = (
            "You extract durable factual claims worth remembering long-term "
            "from an agent session or event. Each claim is one tight prose "
            "paragraph plus a short summary heading. Return only claims that "
            "are durable facts about Wolf, his projects, or his decisions — "
            "not transient task chatter."
        )
        user = (
            f"Harness: {context.harness}. Authored: {context.authored}. "
            f"Day: {context.day}.\n\nSource:\n{session_text}"
        )
        body = self._chat(system, user, _EXTRACT_SCHEMA)
        raw = body.get("candidates")
        if not isinstance(raw, list):
            raise ConsolidationError("extract response missing 'candidates' array")
        candidates: list[RawCandidate] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ConsolidationError("extract candidate is not an object")
            summary = item.get("summary")
            claim = item.get("claim")
            if not isinstance(summary, str) or not isinstance(claim, str):
                raise ConsolidationError("extract candidate missing summary/claim")
            tags = [str(t) for t in item.get("tags") or []]
            refs = [str(r) for r in item.get("refs") or []]
            candidates.append(RawCandidate(summary=summary, claim=claim, tags=tags, refs=refs))
        return candidates

    def score_signals(self, candidate: ClaimLike, *, existing_facts: list[str]) -> SignalScores:
        """Score ``candidate`` on the four LLM-derived signals via a chat call."""
        system = (
            "You score a candidate fact on four signals, each a float in "
            "[0, 1]: importance (how much it matters long-term), confidence "
            "(how sure the claim is true), novelty (how new vs. known facts), "
            "and relevance (how often it would help future recall)."
        )
        existing_block = "\n".join(f"- {f}" for f in existing_facts) or "(none)"
        user = f"Candidate claim:\n{candidate.claim}\n\nExisting facts:\n{existing_block}"
        body = self._chat(system, user, _SCORE_SCHEMA)
        try:
            return SignalScores(
                importance=_unit(body["importance"]),
                confidence=_unit(body["confidence"]),
                novelty_llm=_unit(body["novelty"]),
                relevance=_unit(body["relevance"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConsolidationError(f"score response malformed: {exc}") from exc

    def detect_contradiction(
        self, candidate_claim: str, existing_claim: str
    ) -> ContradictionVerdict:
        """Adjudicate whether the candidate contradicts an existing fact."""
        system = (
            "You decide whether a candidate claim directly contradicts an "
            "existing fact. Two claims contradict when they cannot both be "
            "true of the same world. Return a boolean, a one-line rationale, "
            "and a recommended resolution for a human to adjudicate."
        )
        user = f"Candidate claim:\n{candidate_claim}\n\nExisting fact:\n{existing_claim}"
        body = self._chat(system, user, _CONTRADICTION_SCHEMA)
        try:
            return ContradictionVerdict(
                contradicts=bool(body["contradicts"]),
                rationale=str(body["rationale"]),
                recommended_resolution=str(body["recommended_resolution"]),
            )
        except (KeyError, TypeError) as exc:
            raise ConsolidationError(f"contradiction response malformed: {exc}") from exc

    def probe(self) -> None:
        """Verify the daemon is reachable and the model is pulled.

        Names the operator fix (``ollama pull <model>``) on a 404 /
        model-not-found body, mirroring the embedder's probe.
        """
        payload = self._chat_payload(
            "Reply with an empty candidates list.",
            _PROBE_PROMPT,
            _EXTRACT_SCHEMA,
        )
        try:
            response = self._client.post(f"{self._base_url}/api/chat", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ConsolidationError(
                f"ollama unreachable at {self._base_url} — is the daemon running?"
            ) from exc
        except httpx.RequestError as exc:
            raise ConsolidationError(f"ollama probe failed: {exc}") from exc

        if response.status_code == 404 or self._is_model_not_found(response):
            raise ConsolidationError(
                f"model {self._model} not pulled — run: ollama pull {self._model}"
            )
        if response.status_code >= 400:
            raise ConsolidationError(
                f"ollama returned {response.status_code}: {response.text.strip()}"
            )
        # A 2xx with a parseable body is sufficient; we do not assert on the
        # probe's content beyond it being valid chat JSON.
        self._parse_chat_body(response)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ---------------------------------------------------------------- internals

    def _chat_payload(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Assemble the deterministic ``/api/chat`` request payload."""
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "seed": 0},
        }

    def _chat(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """POST a chat request and return the parsed structured-output object."""
        payload = self._chat_payload(system, user, schema)
        try:
            response = self._client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.RequestError as exc:
            raise ConsolidationError(f"ollama request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ConsolidationError(
                f"ollama returned {response.status_code}: {response.text.strip()}"
            )
        return self._parse_chat_body(response)

    @staticmethod
    def _parse_chat_body(response: httpx.Response) -> dict[str, Any]:
        """Parse Ollama's chat envelope and JSON-decode the message content."""
        import json

        try:
            envelope: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ConsolidationError(f"ollama response is not valid JSON: {exc}") from exc
        message = envelope.get("message")
        if not isinstance(message, dict):
            raise ConsolidationError("ollama response missing 'message' object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ConsolidationError("ollama response message has no string content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConsolidationError(f"ollama structured output is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConsolidationError("ollama structured output is not a JSON object")
        return parsed

    @staticmethod
    def _is_model_not_found(response: httpx.Response) -> bool:
        """Return True when the body looks like Ollama's model-not-found shape."""
        try:
            body = response.json()
        except ValueError:
            return "not found" in (response.text or "").lower()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and "not found" in error.lower():
                return True
        return False


def _unit(value: Any) -> float:
    """Coerce ``value`` to a float clamped into [0, 1]."""
    out = float(value)
    if out < 0.0:
        return 0.0
    if out > 1.0:
        return 1.0
    return out
