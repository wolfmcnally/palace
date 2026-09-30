"""HTTP loopback capture server.

Threading HTTP server bound to loopback. Accepts ``POST /capture`` with a
JSON body, validates it, computes the canonical id, enqueues the line for
the writer thread, and returns 202 immediately so the calling hook never
blocks on disk I/O. ``GET /health`` returns liveness + the palace version
for launchd KeepAlive checks and ``bin/palace-capture-status`` probes.

The whole server is hot-path-pure: no LLM imports, no cloud egress, no
extraction. The only enrichment beyond the inbound payload is the id hash
and the daemon-stamped ``ingest_time``.
"""

from __future__ import annotations

import json
import queue
import signal
import sys
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any

import palace
from palace.daemons.capture.config import (
    BOISE_TZ,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_STORE,
    WRITER_QUEUE_MAXSIZE,
)
from palace.daemons.capture.schema import (
    PayloadError,
    build_record,
    to_jsonl_bytes,
    validate_payload,
)
from palace.daemons.capture.writer import WriteRequest, WriterWorker

__all__ = ["CaptureHandler", "CaptureServer", "serve"]


def _now_boise() -> datetime:
    """Return the current time in Wolf's home timezone.

    Centralized so tests can monkeypatch a fixed instant.
    """
    return datetime.now(BOISE_TZ)


class CaptureServer(ThreadingHTTPServer):
    """Threading HTTP server with per-server writer state.

    The handler reads ``store``, ``writer_queue`` and ``writer_worker`` off
    ``self.server`` so tests can boot multiple servers in one process without
    module-level singletons.
    """

    allow_reuse_address = True
    # socketserver's default listen backlog is 5; a burst of concurrent Stop
    # hooks (or the concurrency smoke) overflows it and macOS resets the
    # excess connections instead of queuing them.
    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        store: Path,
    ) -> None:
        super().__init__(server_address, CaptureHandler)
        self.store: Path = store
        self.writer_queue: queue.Queue[object] = queue.Queue(maxsize=WRITER_QUEUE_MAXSIZE)
        self.writer_worker: WriterWorker = WriterWorker(self.writer_queue)
        self.writer_worker.start()

    def shutdown_writer(self) -> None:
        """Stop the writer thread cleanly. Idempotent."""
        if self.writer_worker.is_alive():
            self.writer_worker.stop()
            self.writer_worker.join(timeout=5.0)


class CaptureHandler(BaseHTTPRequestHandler):
    """One handler per request; threaded via :class:`CaptureServer`."""

    # Older versions of BaseHTTPRequestHandler advertise HTTP/1.0; HTTP/1.1
    # gives us connection-keepalive semantics curl expects and matches the
    # loopback hooks we'll wire in 1.3/1.4.
    protocol_version = "HTTP/1.1"

    server: CaptureServer  # narrowed type for mypy

    # ------------------------------------------------------------------ logging
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib signature
        """Silence the default request log; we log accept/reject explicitly."""
        return

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    # ------------------------------------------------------------------ helpers
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802 — stdlib name
        if self.path == "/health":
            self._handle_health()
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown path: {self.path}")

    def do_POST(self) -> None:  # noqa: N802 — stdlib name
        if self.path != "/capture":
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown path: {self.path}")
            return
        self._handle_capture()

    def do_PUT(self) -> None:  # noqa: N802
        self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    # ------------------------------------------------------------------ routes
    def _handle_health(self) -> None:
        self._send_json(HTTPStatus.OK, {"ok": True, "version": palace.__version__})

    def _handle_capture(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        # Accept ``application/json`` with or without a parameters suffix
        # (e.g., ``; charset=utf-8``); reject everything else.
        if content_type.split(";", 1)[0].strip() != "application/json":
            self._send_error_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return
        try:
            length = int(length_header)
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length < 0:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "negative Content-Length")
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}")
            return

        try:
            payload = validate_payload(payload)
        except PayloadError as exc:
            self._log(
                f"capture: reject status={exc.status} message={exc.message!r}",
            )
            self._send_error_json(exc.status, exc.message)
            return

        now = _now_boise()
        record = build_record(payload, now.isoformat(timespec="seconds"))
        line = to_jsonl_bytes(record)
        req = WriteRequest(
            store=self.server.store,
            day=now.date(),
            session_id=record.session_id,
            line=line,
        )
        try:
            self.server.writer_queue.put(req, timeout=1.0)
        except queue.Full:
            self._log("capture: writer queue full; dropping request")
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "writer queue full",
            )
            return

        if record.event_type == "subagent_stop":
            self._log(
                f"capture: accept event_type=subagent_stop harness={record.harness} "
                f"session_id={record.session_id} agent_type={record.agent_type} "
                f"agent_id={record.agent_id} id={record.id}"
            )
        else:
            self._log(
                f"capture: accept event_type=stop harness={record.harness} "
                f"session_id={record.session_id} id={record.id}"
            )
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "id": record.id})


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    store: Path = DEFAULT_STORE,
) -> int:
    """Run the capture daemon in the foreground until SIGINT/SIGTERM.

    Returns the exit code (always 0 on clean shutdown).
    """
    store.mkdir(parents=True, exist_ok=True)
    server = CaptureServer((host, port), store)
    bound_host = str(server.server_address[0])
    bound_port = int(server.server_address[1])
    print(
        f"palace capture: listening on http://{bound_host}:{bound_port} store={store}",
        file=sys.stderr,
        flush=True,
    )

    stop_event = threading.Event()

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        print(
            f"palace capture: received signal {signum}; shutting down",
            file=sys.stderr,
            flush=True,
        )
        stop_event.set()
        # ``shutdown`` must be called from a different thread than
        # ``serve_forever``; run it via a one-shot helper thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.shutdown_writer()
        server.server_close()
    return 0
