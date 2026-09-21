#!/usr/bin/env python3
"""Answer relay between the Obvious agent sessions and the review pipeline.

Why this exists: the Obvious External Developer API starts agent sessions
that run on Obvious's infrastructure, and the only documented way an answer
comes back is the agent itself making an HTTP call (successPrompt /
failurePrompt). The review pipeline runs on a single operator's machine -
127.0.0.1, no inbound exposure - so the agent cannot POST answers straight
to it. This relay is the small public side of that handshake:

    Obvious agent ── POST /answers/{session_id}?token=... ──▶ relay (public)
    pipeline      ── GET  /answers/{session_id}             ──▶ relay (outbound poll)

Design rules:

- FIRST WRITE WINS. A session id is a fresh uuid4 per research call. If the
  agent posts an answer and later posts a failure report to the same session,
  the first delivery is kept and the second gets 409 - a late failurePrompt
  can never overwrite a delivered answer.
- TOKEN REQUIRED. The shared token is checked on every write and read (query
  param or X-Relay-Token header). Without a configured token the relay refuses
  everything - it fails closed, never open.
- ANSWERS ARE SMALL. Bodies are capped at 256 KiB (a research answer is a few
  KB of JSON); anything larger is rejected.
- DISK-PERSISTENT, STATELESS OTHERWISE. Each answer is one atomic file under
  the data dir; the process can restart without losing undelivered answers.

Zero dependencies beyond the Python standard library, so it can run anywhere:
    OBVIOUS_RELAY_TOKEN=... python3 relay/obvious_relay.py --port 8801
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, SplitResult, parse_qs, urlparse

# A research answer is a few KB of JSON; 256 KiB is generous headroom.
MAX_BODY_BYTES = 256 * 1024

# Session ids are hex (uuid4().hex is 32 chars); keep some slack for testing.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def make_handler(token: str, data_dir: Path) -> type[BaseHTTPRequestHandler]:
    answers_dir = data_dir / "answers"

    def _authorized(parsed: ParseResult | SplitResult, headers: Any) -> bool:
        supplied = ""
        query = parse_qs(parsed.query)
        if query.get("token"):
            supplied = query["token"][0]
        elif headers.get("X-Relay-Token"):
            supplied = headers["X-Relay-Token"]
        return bool(token) and supplied == token

    class RelayHandler(BaseHTTPRequestHandler):
        """One handler per request; token/data_dir close over the factory."""

        server_version = "obvious-relay/1.0"

        # -- helpers ---------------------------------------------------- #
        def _session(self) -> str | None:
            sid = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            return sid if SESSION_ID_RE.match(sid) else None

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- routes ----------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True, "token_required": bool(token)})
                return
            if parsed.path.startswith("/answers/"):
                if not _authorized(parsed, self.headers):
                    self._send(403, {"error": "relay token required"})
                    return
                sid = self._session()
                if sid is None:
                    self._send(404, {"error": "unknown session"})
                    return
                answer_file = answers_dir / f"{sid}.answer"
                if answer_file.exists():
                    self._send(
                        200,
                        {"state": "answered", "answer": answer_file.read_text("utf-8")},
                    )
                else:
                    self._send(200, {"state": "pending"})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/answers/"):
                self._send(404, {"error": "not found"})
                return
            if not _authorized(parsed, self.headers):
                self._send(403, {"error": "relay token required"})
                return
            sid = self._session()
            if sid is None:
                self._send(400, {"error": "session id must be 8-64 hex chars"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"error": "answer too large"})
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            if not body:
                self._send(400, {"error": "empty answer"})
                return

            answers_dir.mkdir(parents=True, exist_ok=True)
            answer_file = answers_dir / f"{sid}.answer"
            if answer_file.exists():
                # First write wins: a late failurePrompt can never overwrite
                # an answer that was already delivered.
                self._send(409, {"error": "answer already stored", "state": "answered"})
                return
            fd, tmp_path = tempfile.mkstemp(dir=str(answers_dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp_path, answer_file)
            self._send(201, {"state": "stored", "session_id": sid})

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
            # Keep relay logs compact: method, path, result.
            print(f"relay: {self.address_string()} {fmt % args}")

    return RelayHandler


def serve(host: str, port: int, token: str, data_dir: Path) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(token, data_dir))
    print(f"obvious-relay listening on {host}:{port} (data: {data_dir})")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Obvious answer relay")
    parser.add_argument("--host", default=os.environ.get("RELAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RELAY_PORT", "8801")))
    parser.add_argument(
        "--token",
        default=os.environ.get("OBVIOUS_RELAY_TOKEN", os.environ.get("RELAY_TOKEN", "")),
        help="shared token (env OBVIOUS_RELAY_TOKEN); refuse all traffic when empty",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("RELAY_DATA_DIR", str(Path(__file__).parent / "relay_data")),
    )
    args = parser.parse_args()

    if not args.token:
        raise SystemExit(
            "OBVIOUS_RELAY_TOKEN is not set - the relay refuses all traffic "
            "without a token. Set it in the environment or pass --token."
        )
    server = serve(args.host, args.port, args.token, Path(args.data_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nrelay stopped.")


if __name__ == "__main__":
    main()
