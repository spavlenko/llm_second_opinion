"""A small OpenAI-compatible server that replays recorded chat completions in order.

Lets contributors and CI run end-to-end tests without a GPU or API keys.

Recordings are JSONL, one completion per line, answered in file order:

    {"message": {"content": "...", "tool_calls": [...]}, "finish_reason": "stop", "usage": {...}}

With `upstream`, requests past the end of the file are proxied to a real endpoint and
appended to it, which is how recordings are made.
"""

from __future__ import annotations

import json
import socketserver
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


def load_recordings(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    try:
        return [json.loads(line) for line in lines if line.strip()]
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: {e}") from e


class MockServer(HTTPServer):
    """Single-threaded on purpose: an agent sends one request at a time."""

    def __init__(
        self,
        address: tuple[str, int],
        recordings: Path,
        upstream: str | None = None,
        upstream_api_key: str | None = None,
    ):
        super().__init__(address, _Handler)
        self.recordings_path = recordings
        self.recordings = load_recordings(recordings)
        self.served = 0
        self.upstream = upstream.rstrip("/") if upstream else None
        self.upstream_api_key = upstream_api_key

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn, which can stall for tens of
        # seconds on macOS; the server name is never used here.
        socketserver.TCPServer.server_bind(self)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/v1"

    def complete(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if self.served < len(self.recordings):
            entry = self.recordings[self.served]
        elif self.upstream:
            entry = self._fetch_upstream(body)
            self.recordings.append(entry)
            with self.recordings_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        else:
            return None
        self.served += 1
        return entry

    def _fetch_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
        headers = {"Content-Type": "application/json"}
        if self.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.upstream_api_key}"
        req = urllib.request.Request(
            f"{self.upstream}/chat/completions", data=json.dumps(body).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            reply = json.load(resp)
        choice = reply["choices"][0]
        return {
            "message": choice["message"],
            "finish_reason": choice.get("finish_reason"),
            "usage": reply.get("usage"),
        }


class _Handler(BaseHTTPRequestHandler):
    server: MockServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._error(404, f"no route for POST {self.path}")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            self._error(400, "request body is not valid JSON")
            return
        try:
            entry = self.server.complete(body)
        except (OSError, ValueError, LookupError) as e:
            self._error(502, f"upstream request failed: {e}")
            return
        if entry is None:
            self._error(404, f"recordings exhausted after {self.server.served} completion(s)")
            return

        message = {"role": "assistant", "content": None, **entry["message"]}
        finish = entry.get("finish_reason") or (
            "tool_calls" if message.get("tool_calls") else "stop"
        )
        usage = entry.get("usage") or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        base = {
            "id": f"chatcmpl-mock-{self.server.served}",
            "created": int(time.time()),
            "model": body.get("model", "mock"),
        }

        if not body.get("stream"):
            choice = {"index": 0, "message": message, "finish_reason": finish}
            self._json(
                200, {**base, "object": "chat.completion", "choices": [choice], "usage": usage}
            )
            return

        deltas = [{"role": "assistant", "content": message["content"] or ""}]
        deltas += [
            {"tool_calls": [{"index": i, **call}]}
            for i, call in enumerate(message.get("tool_calls") or [])
        ]
        events = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
        events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if (body.get("stream_options") or {}).get("include_usage"):
            events.append({"choices": [], "usage": usage})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            payload = {**base, "object": "chat.completion.chunk", **event}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "mock_error", "code": status}})
