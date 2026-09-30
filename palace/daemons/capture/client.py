"""Fire-and-forget client for ``palace capture post``.

Reads a JSON payload from a file path (or ``-`` for stdin), POSTs it to the
running capture daemon, and exits non-zero with a stderr warning on connection
failures rather than raising into the caller. Phase 1.4 and 1.5 hooks layer
on top of this contract — a dead daemon must not break the agent session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from palace.daemons.capture.config import DEFAULT_HOST, DEFAULT_PORT

__all__ = ["post_payload"]


def _load_payload(source: str) -> Any:
    data = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(data)


def post_payload(
    payload_source: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 2.0,
) -> int:
    """POST the JSON at ``payload_source`` to the capture daemon.

    Returns 0 on a 2xx response, 1 on a server-side rejection (4xx/5xx), and
    2 on a connection-level failure (unreachable / timeout / transport).
    Never raises: hook integrations depend on graceful failure.
    """
    try:
        payload = _load_payload(payload_source)
    except FileNotFoundError as exc:
        print(f"error: payload file not found: {exc}", file=sys.stderr, flush=True)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: payload is not valid JSON: {exc}", file=sys.stderr, flush=True)
        return 1

    url = f"http://{host}:{port}/capture"
    try:
        response = httpx.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        print(
            f"warning: capture daemon unreachable at {url}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    if 200 <= response.status_code < 300:
        print(response.text, flush=True)
        return 0

    print(
        f"warning: capture daemon returned HTTP {response.status_code}: {response.text}",
        file=sys.stderr,
        flush=True,
    )
    return 1
