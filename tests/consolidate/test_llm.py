"""Synthetic proof of the consolidator's extraction prompt.

The injected ``httpx.MockTransport`` answers in-process; no network, model or
provider is contacted. The proof that nothing leaves the process is the
transport itself plus the one-request oracle.
"""

from __future__ import annotations

import json

import httpx

from palace.consolidate.llm import ExtractionContext, OllamaConsolidatorLLM

EXPECTED_SYSTEM_PROMPT = (
    "You extract durable factual claims worth remembering long-term "
    "from an agent session or event. Each claim is one tight prose "
    "paragraph plus a short summary heading. Return only claims that "
    "are durable facts about the user, their projects, or their decisions — "
    "not transient task chatter."
)


def test_extraction_prompt_targets_the_user_generically() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = json.dumps({"candidates": []})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    llm = OllamaConsolidatorLLM(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    )
    try:
        result = llm.extract_candidates(
            "A synthetic one-line session.",
            context=ExtractionContext(harness="claude-code", authored=True, day="2000-01-01"),
        )
    finally:
        llm.close()

    assert result == []
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://ollama.test/api/chat"
    messages = json.loads(request.content)["messages"]
    assert messages[0] == {"role": "system", "content": EXPECTED_SYSTEM_PROMPT}
