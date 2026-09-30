"""Synthetic HTTPS endpoint for tests and the private-provider walkthrough."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from palace.multistore_demo import DemoEmbedder, DemoReranker
from palace.provider_config import EndpointConfig, write_selector

TOKEN = "synthetic-private-demo-only"
ENV = "PALACE_PRIVATE_DEMO_TOKEN"
NAME = "fixture-endpoint"
MARKER = ".palace-private-demo.json"


def certificates(root: Path) -> tuple[Path, Path]:
    cert, key = root / "cert.pem", root / "key.pem"
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("the HTTPS fixture requires openssl")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    key.chmod(0o600)
    return cert, key


class FixtureServer:
    """A bounded-lifetime server; request records stay only in process memory."""

    def __init__(self, root: Path, *, port: int = 0, create_certificates: bool = True) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.cert, self.key = (
            certificates(root) if create_certificates else (root / "cert.pem", root / "key.pem")
        )
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413)
                    return
                request = json.loads(self.rfile.read(length))
                owner.requests.append(request)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self.send_error(401)
                    return
                if request["operation"] == "embedding":
                    vectors = DemoEmbedder().embed(request["input"])
                    data = [{"index": i, "embedding": vector} for i, vector in enumerate(vectors)]
                else:
                    scores = DemoReranker().score(request["query"], request["documents"])
                    data = [
                        {"index": i, "relevance_score": score} for i, score in enumerate(scores)
                    ]
                body = json.dumps(
                    {"provider": NAME, "model": request["model"], "data": data}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_port}/inference"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self) -> FixtureServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("HTTPS fixture did not stop")

    def config(self) -> EndpointConfig:
        return EndpointConfig(
            name=NAME,
            url=self.url,
            credential_env=ENV,
            adapter="palace-json-v1",
            authorized_boundary="operator-controlled",
            boundary_note="Synthetic local HTTPS fixture",
            boundary_asserted_at="2026-09-06T00:00:00Z",
            timeout_seconds=2,
            max_attempts=1,
            ca_bundle=str(self.cert),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "serve", "cleanup"))
    parser.add_argument("--port", type=int, default=18443)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    base = root / ".private-demo"
    try:
        if not (root / "bin/palace-private-demo").is_file():
            raise ValueError("run the fixture driver from its repository checkout")
        if args.action == "setup":
            if base.exists() or base.is_symlink():
                raise ValueError(".private-demo already exists; nothing overwritten")
            base.mkdir()
            certificates(base)
            source = base / "source"
            source.mkdir()
            (source / "retention.md").write_text(
                "## Retention policy\nKeep records for seven years.\n"
            )
            endpoint = EndpointConfig(
                name=NAME,
                url=f"https://127.0.0.1:{args.port}/inference",
                credential_env=ENV,
                adapter="palace-json-v1",
                authorized_boundary="operator-controlled",
                boundary_note="Synthetic local HTTPS fixture",
                boundary_asserted_at="2026-09-06T00:00:00Z",
                ca_bundle=str(base / "cert.pem"),
                max_attempts=1,
                timeout_seconds=2,
            )
            write_selector(
                base / "embedding.toml",
                {
                    "provider": "endpoint",
                    "model": "fixture-embedding",
                    "dim": 4096,
                    "endpoint": endpoint.document(),
                },
            )
            write_selector(
                base / "reranking.toml",
                {
                    "provider": "endpoint",
                    "model": "fixture-reranker",
                    "endpoint": endpoint.document(),
                },
            )
            immutable = {
                str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (source / "retention.md", base / "cert.pem", base / "key.pem")
            }
            (base / MARKER).write_text(
                json.dumps({"root": str(base), "port": args.port, "immutable": immutable}) + "\n"
            )
            print("Created synthetic HTTPS fixture. Follow docs/private-providers.md next.")
            return 0
        if base.is_symlink():
            raise ValueError("fixture root is a symlink")
        marker = json.loads((base / MARKER).read_text())
        if marker.get("root") != str(base):
            raise ValueError("fixture marker does not match this directory")
        if args.action == "serve":
            with FixtureServer(base, port=marker["port"], create_certificates=False) as server:
                print(
                    f"Synthetic HTTPS endpoint listening at {server.url}; Ctrl-C stops it.",
                    flush=True,
                )
                with suppress(KeyboardInterrupt):
                    threading.Event().wait()
            return 0
        with socket.socket() as probe:
            probe.settimeout(1)
            connection = probe.connect_ex(("127.0.0.1", marker["port"]))
            if connection != errno.ECONNREFUSED:
                raise ValueError("fixture server is listening or its stopped state is unknown")
        allowed = re.compile(
            r"(?:embedding\.toml|reranking\.toml|cert\.pem|key\.pem|source/retention\.md|store/meta/(?:(?:embedder|reranker)\.toml|schema-version)|store/index/chunks\.sqlite(?:-wal|-shm|-journal)?|store/events/cloud-egress/\d{4}-\d{2}-\d{2}\.jsonl)\Z"
        )
        for member in base.rglob("*"):
            if member.is_symlink():
                raise ValueError("fixture has symlinks; cleanup refused")
            relative = member.relative_to(base).as_posix()
            if member.is_file() and relative != MARKER and not allowed.fullmatch(relative):
                raise ValueError("fixture has unexpected files; cleanup refused")
        for name, digest in marker["immutable"].items():
            if hashlib.sha256((base / name).read_bytes()).hexdigest() != digest:
                raise ValueError("fixture source or certificate changed; cleanup refused")
        shutil.rmtree(base)
        print("Removed the marked synthetic fixture; stop its server before cleanup.")
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
