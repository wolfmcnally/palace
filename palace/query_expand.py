"""Opt-in local query expansion through Ollama structured chat output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from palace.index._errors import IndexError
from palace.retrieval_config import EXPANSION_MODEL, OLLAMA_BASE_URL

__all__ = ["OllamaQueryExpander", "QueryExpander"]


class QueryExpander(Protocol):
    """The HyDE and multi-query contract used by retrieval."""

    def hypothetical_passage(self, query: str) -> str: ...

    def rewrites(self, query: str, n: int) -> list[str]: ...

    def probe(self) -> None: ...

    def close(self) -> None: ...


_HYDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"passage": {"type": "string"}},
    "required": ["passage"],
}

_REWRITES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}

_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


class OllamaQueryExpander:
    """Deterministic HyDE and query rewrites using one local HTTP client."""

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = EXPANSION_MODEL,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._closed = False

    def hypothetical_passage(self, query: str) -> str:
        """Generate one concise passage that would answer ``query``."""
        body = self._chat(
            "Write a concise hypothetical source passage that directly answers the query. "
            "Return only the requested JSON object.",
            query,
            _HYDE_SCHEMA,
        )
        passage = body.get("passage")
        if not isinstance(passage, str) or not passage.strip():
            raise IndexError("HyDE response contained no usable passage")
        return passage.strip()

    def rewrites(self, query: str, n: int) -> list[str]:
        """Generate at most ``n`` non-empty, case-insensitively unique rewrites."""
        if n <= 0:
            raise IndexError("multi_query must be positive")
        body = self._chat(
            f"Rewrite the user's query in exactly {n} distinct ways for document retrieval. "
            "Preserve names, identifiers, and intent. Return only the requested JSON object.",
            query,
            _REWRITES_SCHEMA,
        )
        raw_queries = body.get("queries")
        if not isinstance(raw_queries, Sequence) or isinstance(raw_queries, (str, bytes)):
            raise IndexError("multi-query response missing 'queries' array")

        seen: set[str] = set()
        rewrites: list[str] = []
        for value in raw_queries:
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            key = stripped.casefold()
            if not stripped or key in seen:
                continue
            seen.add(key)
            rewrites.append(stripped)
            if len(rewrites) == n:
                break
        if not rewrites:
            raise IndexError("multi-query response contained no usable rewrites")
        return rewrites

    def probe(self) -> None:
        """Verify Ollama reachability and that the configured model is pulled."""
        payload = self._chat_payload(
            "Return the requested JSON object.",
            "__palace_query_expand_probe__",
            _PROBE_SCHEMA,
        )
        try:
            response = self._client.post(f"{self._base_url}/api/chat", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise IndexError(
                f"ollama unreachable at {self._base_url} — is the daemon running?"
            ) from exc
        except httpx.RequestError as exc:
            raise IndexError(f"ollama probe failed: {exc}") from exc
        if response.status_code == 404 or self._is_model_not_found(response):
            raise IndexError(f"model {self._model} not pulled — run: ollama pull {self._model}")
        if response.status_code >= 400:
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")
        self._parse_chat_body(response)

    def close(self) -> None:
        """Close the HTTP client; safe to call more than once."""
        if not self._closed:
            self._client.close()
            self._closed = True

    def _chat_payload(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
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
        try:
            response = self._client.post(
                f"{self._base_url}/api/chat",
                json=self._chat_payload(system, user, schema),
            )
        except httpx.RequestError as exc:
            raise IndexError(f"ollama request failed: {exc}") from exc
        if response.status_code >= 400:
            raise IndexError(f"ollama returned {response.status_code}: {response.text.strip()}")
        return self._parse_chat_body(response)

    @staticmethod
    def _parse_chat_body(response: httpx.Response) -> dict[str, Any]:
        try:
            envelope: dict[str, Any] = response.json()
        except ValueError as exc:
            raise IndexError(f"ollama response is not valid JSON: {exc}") from exc
        message = envelope.get("message")
        if not isinstance(message, dict):
            raise IndexError("ollama response missing 'message' object")
        content = message.get("content")
        if not isinstance(content, str):
            raise IndexError("ollama response message has no string content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise IndexError(f"ollama structured output is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IndexError("ollama structured output is not a JSON object")
        return parsed

    @staticmethod
    def _is_model_not_found(response: httpx.Response) -> bool:
        try:
            body = response.json()
        except ValueError:
            return "not found" in (response.text or "").lower()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and "not found" in error.lower():
                return True
        return False
