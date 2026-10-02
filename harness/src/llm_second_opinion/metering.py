"""The metering proxy: every model call a work item makes goes through it, and it records the
provider's own token counts.

One proxy serves a batch. The runner registers each item (with its `usage.jsonl` and the
real endpoint per role) and gives the agent proxy URLs, `/items/<id>/<role>/v1`. The proxy
forwards each call with the API key and secret headers from the host environment, so
containers hold no secrets, and appends one `UsageRecord` per call. Counts come only from the
provider's `usage`: streaming requests are made to include it, and a successful call without
it marks the item failed. Nothing is estimated.

Not the mock server's upstream mode: that one is single-threaded, buffers whole responses,
and drops streaming; here parallel items stream through unchanged.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from llm_second_opinion.config import ConfigError, Price
from llm_second_opinion.contracts import ModelEndpoint, Role, RoleUsage, RunConfig, UsageRecord

CONTAINER_HOST = "host.docker.internal"  # how task containers reach the Mac (host gateway)
UPSTREAM_TIMEOUT_S = 600  # per socket read; a reasoning model can think for minutes
DRAIN_TIMEOUT_S = 600  # how long an item waits for its in-flight calls after the agent stops
ROLES: tuple[Role, ...] = ("executor", "advisor")
PREFLIGHT_HEADER = "X-LSO-Preflight"  # lets the mock server answer without using a recording

_ROUTE = re.compile(r"^/items/(?P<item>[0-9a-f]+)/(?P<role>executor|advisor)/v1(?P<rest>/.*)?$")
# Not forwarded: hop-by-hop headers, and what the proxy sets itself.
_DROP_REQUEST = {
    "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding", "authorization",
}  # fmt: skip
_DROP_RESPONSE = {
    "connection", "keep-alive", "proxy-authenticate", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length", "server", "date",
}  # fmt: skip


class MeteringError(RuntimeError):
    """An item's usage is incomplete, so it must not be recorded as done."""


class PreflightError(ConfigError):
    """An endpoint does not report usage; the batch is refused before it starts."""


# --- usage parsing ---------------------------------------------------------------


class Tokens(NamedTuple):
    prompt: int
    completion: int
    cached: int = 0
    reasoning: int = 0


def usage_tokens(payload: Any) -> Tokens | None:
    """Token counts from a response body or stream chunk, or None if it has no usage.

    Reads Chat Completions' `usage` (with `prompt_tokens_details.cached_tokens` and
    `completion_tokens_details.reasoning_tokens`; Moonshot puts `cached_tokens` at the top
    level) and the Responses API's `input_tokens`/`output_tokens`, also nested in a
    `response.completed` event.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if usage is None and isinstance(payload.get("response"), dict):
        usage = payload["response"].get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if not (_count(prompt) and _count(completion)):
        return None
    inputs = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    outputs = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    cached = inputs.get("cached_tokens") if isinstance(inputs, dict) else None
    if cached is None:
        cached = usage.get("cached_tokens")
    reasoning = outputs.get("reasoning_tokens") if isinstance(outputs, dict) else None
    return Tokens(
        prompt,
        completion,
        cached if _count(cached) else 0,
        reasoning if _count(reasoning) else 0,
    )


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class StreamUsage:
    """Watches a server-sent event stream line by line and keeps the last usage seen
    (with `include_usage`, every chunk carries `usage: null` except the final one)."""

    def __init__(self) -> None:
        self.tokens: Tokens | None = None

    def feed(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            tokens = usage_tokens(json.loads(data))
        except ValueError:
            return
        if tokens is not None:
            self.tokens = tokens


def body_tokens(body: bytes, encoding: str | None) -> Tokens | None:
    """Usage from a whole (non-streaming) response body."""
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        return usage_tokens(json.loads(body))
    except (ValueError, OSError):
        return None


def force_stream_usage(body: dict[str, Any]) -> dict[str, Any]:
    """A streaming completion request with `stream_options.include_usage` turned on."""
    if body.get("stream") is not True:
        return body
    options = body.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    return {**body, "stream_options": {**options, "include_usage": True}}


# --- per-item bookkeeping ----------------------------------------------------------


@dataclass
class ItemMeter:
    """One item's routes, usage file, running totals, and budget. Shared by handler threads."""

    id: str
    endpoints: dict[str, ModelEndpoint]
    usage_path: Path
    max_tokens: int | None = None
    calls: int = 0
    tokens: int = 0
    refused: int = 0  # calls answered with the budget error
    missing: list[str] = field(default_factory=list)  # successful calls without usage
    _in_flight: int = 0
    _closed: bool = False
    _cond: threading.Condition = field(default_factory=threading.Condition)

    def __post_init__(self) -> None:
        # Each attempt starts its own record; the ledger keeps only the last attempt.
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text("")

    def begin(self) -> tuple[int, str, str] | None:
        """Admit a model call, or return the refusal (HTTP status, error type, message).

        The budget refusal is a 403: clients retry 429 and 5xx, and a retry cannot help.
        """
        with self._cond:
            if self._closed:
                return 410, "proxy_error", "this item's run has ended"
            if self.max_tokens is not None and self.tokens >= self.max_tokens:
                self.refused += 1
                message = (
                    f"token budget spent: this item used {self.tokens} of its "
                    f"{self.max_tokens} tokens (limits.max_tokens)"
                )
                return 403, "token_limit", message
            self._in_flight += 1
            return None

    def end(self, role: Role, model: str, tokens: Tokens | None, status: int, start: float,
            note: str) -> None:  # fmt: skip
        """Record a finished call: a usage line, or a missing-usage mark for a 2xx call."""
        with self._cond:
            try:
                if tokens is not None:
                    record = UsageRecord(
                        seq=self.calls,
                        ts=start,
                        role=role,
                        model=model,
                        prompt_tokens=tokens.prompt,
                        completion_tokens=tokens.completion,
                        cached_tokens=tokens.cached,
                        reasoning_tokens=tokens.reasoning,
                        latency_ms=(time.time() - start) * 1000,
                        status=status,
                    )
                    with self.usage_path.open("a") as f:
                        f.write(record.model_dump_json() + "\n")
                    self.calls += 1
                    self.tokens += tokens.prompt + tokens.completion
                elif 200 <= status < 300:
                    self.missing.append(f"{role} call {note}: HTTP {status} without usage")
            finally:
                self._in_flight -= 1
                self._cond.notify_all()

    def close(self, timeout_s: float = DRAIN_TIMEOUT_S) -> None:
        """Refuse new calls and wait for those in flight (their usage is still to come).
        Raises MeteringError if the usage is incomplete."""
        with self._cond:
            self._closed = True
            drained = self._cond.wait_for(lambda: self._in_flight == 0, timeout_s)
            if not drained:
                raise MeteringError(f"{self._in_flight} model call(s) still running after the run")
            if self.missing:
                raise MeteringError(
                    f"{len(self.missing)} model call(s) without usage, so the item has no "
                    f"exact token count: {'; '.join(self.missing[:3])}"
                )


# --- the proxy -----------------------------------------------------------------------


class MeteringProxy(ThreadingHTTPServer):
    """One per batch. Binds 127.0.0.1 by default: Docker Desktop forwards the containers'
    `host.docker.internal` to the Mac's loopback, so nothing else on the network can use
    the proxy (and with it, the keys)."""

    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, env: Mapping[str, str] = os.environ):
        super().__init__((host, port), _Handler)
        self.env = env
        self.items: dict[str, ItemMeter] = {}
        self._lock = threading.Lock()

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn, which can stall on macOS.
        socketserver.TCPServer.server_bind(self)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self) -> MeteringProxy:
        threading.Thread(target=self.serve_forever, args=(0.1,), daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def register(
        self, endpoints: dict[str, ModelEndpoint], usage_path: Path, max_tokens: int | None
    ) -> ItemMeter:
        """Open routes for one item attempt; the id is random, so no item can guess another's."""
        meter = ItemMeter(secrets.token_hex(8), endpoints, usage_path, max_tokens)
        with self._lock:
            self.items[meter.id] = meter
        return meter

    def unregister(self, meter: ItemMeter) -> None:
        with self._lock:
            self.items.pop(meter.id, None)

    def url(self, meter: ItemMeter, role: Role, host: str = CONTAINER_HOST) -> str:
        return f"http://{host}:{self.port}/items/{meter.id}/{role}/v1"


def rewrite(config: RunConfig, proxy: MeteringProxy, meter: ItemMeter) -> RunConfig:
    """The run config the agent sees: each model behind its proxy route, with no key
    names or headers (the proxy adds them). The config hash is unchanged."""

    def routed(endpoint: ModelEndpoint | None, role: Role) -> ModelEndpoint | None:
        if endpoint is None:
            return None
        return endpoint.model_copy(
            update={
                "base_url": proxy.url(meter, role),
                "api_key_env": None,
                "headers": {},
                "header_env": {},
            }
        )

    return config.model_copy(
        update={
            "executor": routed(config.executor, "executor"),
            "advisor_model": routed(config.advisor_model, "advisor"),
        }
    )


def endpoint_headers(endpoint: ModelEndpoint, env: Mapping[str, str]) -> dict[str, str]:
    """The endpoint's headers with secrets filled in from `env`: what the proxy adds."""
    headers = dict(endpoint.headers)
    headers |= {name: env[var] for name, var in endpoint.header_env.items()}
    if endpoint.api_key_env:
        headers["Authorization"] = f"Bearer {env[endpoint.api_key_env]}"
    return headers


class _Handler(BaseHTTPRequestHandler):
    server: MeteringProxy
    # HTTP/1.0: each response ends by closing the connection, so a stream needs no framing.
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        path, _, query = self.path.partition("?")
        route = _ROUTE.match(path)
        meter = self.server.items.get(route["item"]) if route else None
        role = route["role"] if route else None
        endpoint = meter.endpoints.get(role) if meter else None
        if endpoint is None:
            self._error(404, "proxy_error", f"no route for {self.command} {path}")
            return
        body = self._read_body()
        model = endpoint.model
        if self.command == "POST" and body:
            try:
                request = json.loads(body)
            except ValueError:
                request = None
            if isinstance(request, dict):
                model = str(request.get("model") or model)
                forced = force_stream_usage(request)
                if forced is not request:
                    body = json.dumps(forced).encode()
        target = (
            endpoint.base_url.rstrip("/") + (route["rest"] or "") + (f"?{query}" if query else "")
        )
        if self.command != "POST":  # e.g. GET /models: not a model call, nothing to meter
            self._relay(target, endpoint, body, None)
            return
        refusal = meter.begin()
        if refusal:
            self._error(*refusal)
            return
        start = time.time()
        tokens, status = None, 0
        try:
            tokens, status = self._relay(target, endpoint, body, StreamUsage())
        finally:
            meter.end(role, model, tokens, status, start, f"to {route['rest'] or '/'}")

    def _relay(
        self, target: str, endpoint: ModelEndpoint, body: bytes, usage: StreamUsage | None
    ) -> tuple[Tokens | None, int]:
        """Forward the request and pass the response back as it arrives."""
        try:
            secret = endpoint_headers(endpoint, self.server.env)
        except KeyError as e:
            self._error(500, "proxy_error", f"secret variable {e.args[0]} is not set for the proxy")
            return None, 0
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_REQUEST}
        headers = {k: v for k, v in headers.items() if k.lower() not in {h.lower() for h in secret}}
        headers |= {"Accept-Encoding": "identity", **secret}
        url = urlsplit(target)
        conn_type = (
            http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_type(url.netloc, timeout=UPSTREAM_TIMEOUT_S)
        try:
            conn.request(
                self.command,
                url.path + (f"?{url.query}" if url.query else ""),
                body or None,
                headers,
            )
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            self._error(502, "proxy_error", f"upstream request failed: {type(e).__name__}: {e}")
            return None, 0
        try:
            return self._stream_back(resp, usage), resp.status
        except (OSError, http.client.HTTPException):
            # Cut off mid-response: whatever usage arrived counts; none marks the call.
            return (usage.tokens if usage else None), resp.status
        finally:
            conn.close()

    def _stream_back(
        self, resp: http.client.HTTPResponse, usage: StreamUsage | None
    ) -> Tokens | None:
        """Send status, headers, and body to the client, reading usage on the way. If the
        client goes away mid-stream (an agent aborting at its limit), keep reading: the
        provider bills the whole call, and only the end of the stream says how much."""
        self.send_response(resp.status, resp.reason)
        for name, value in resp.getheaders():
            if name.lower() not in _DROP_RESPONSE:
                self.send_header(name, value)
        streaming = (resp.getheader("Content-Type") or "").startswith("text/event-stream")
        if not streaming:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._send(data)
            return body_tokens(data, resp.getheader("Content-Encoding")) if usage else None
        self.end_headers()
        client = True
        while line := resp.readline():
            if usage:
                usage.feed(line)
            client = client and self._send(line)
        return usage.tokens if usage else None

    def _send(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except OSError:
            return False

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0].strip() or b"0", 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            while self.rfile.readline().strip():  # trailers
                pass
            return b"".join(chunks)
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _error(self, status: int, kind: str, message: str) -> None:
        data = json.dumps({"error": {"message": message, "type": kind, "code": status}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


# --- preflight -------------------------------------------------------------------------


def preflight(proxy: MeteringProxy, endpoints: Mapping[str, ModelEndpoint], out_dir: Path) -> None:
    """Send one tiny streaming request to each endpoint through the proxy (the path every
    call takes) and refuse the batch if any answer lacks usage. The proxy is asked for
    usage, not the request, so this also checks that forcing `include_usage` works."""
    problems = []
    for key, endpoint in endpoints.items():
        meter = proxy.register({"executor": endpoint}, out_dir / f"{key}.usage.jsonl", None)
        try:
            error = _preflight_call(proxy.url(meter, "executor", "127.0.0.1"), endpoint.model)
            if error is None:
                meter.close(UPSTREAM_TIMEOUT_S)
                if not meter.calls:
                    error = "no response was recorded"
        except MeteringError as e:
            error = str(e)
        finally:
            proxy.unregister(meter)
        if error:
            problems.append(f"{key} ({endpoint.model}): {error}")
    if problems:
        raise PreflightError(
            "usage preflight failed; only endpoints that report token usage (also when "
            "streaming) can be used:\n  " + "\n  ".join(problems)
        )


def _preflight_call(url: str, model: str) -> str | None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "stream": True,
    }
    request = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", PREFLIGHT_HEADER: "1"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=UPSTREAM_TIMEOUT_S) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


# --- totals and cost ---------------------------------------------------------------------


def read_usage(path: Path) -> list[UsageRecord]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [UsageRecord.model_validate_json(line) for line in lines if line.strip()]


def summarize_usage(records: Iterable[UsageRecord]) -> list[RoleUsage]:
    """Per-role totals, for `AgentResult.usage`; a role with no calls is left out."""
    counts = ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens")
    totals: dict[str, dict[str, int]] = {}
    for r in records:
        t = totals.setdefault(r.role, dict.fromkeys(("calls", *counts), 0))
        t["calls"] += 1
        for name in counts:
            t[name] += getattr(r, name)
    return [RoleUsage(role=role, **totals[role]) for role in ROLES if role in totals]


def cost_usd(usage: list[RoleUsage], prices: Mapping[str, Price | None]) -> float | None:
    """Cost of an item's calls from per-role prices (role -> price of the model it used).

    Cached prompt tokens are charged at the cached price when one is set. Reasoning tokens
    are part of the completion tokens, so they are charged at the output price. None if a
    role that made calls has no price: a partial cost would read as a real one.
    """
    total = 0.0
    for u in usage:
        price = prices.get(u.role)
        if price is None:
            return None
        cached = u.cached_tokens if price.cached_input_per_mtok is not None else 0
        total += (
            (u.prompt_tokens - cached) * price.input_per_mtok
            + cached * (price.cached_input_per_mtok or 0.0)
            + u.completion_tokens * price.output_per_mtok
        ) / 1e6
    return total


def ledger_fields(usage: list[RoleUsage], cost: float | None) -> dict[str, Any]:
    """The ledger's token columns for a metered item (zeros for a role without calls)."""
    by_role = {u.role: u for u in usage}
    fields: dict[str, Any] = {}
    for role in ROLES:
        u = by_role.get(role)
        fields[f"{role}_prompt_tokens"] = u.prompt_tokens if u else 0
        fields[f"{role}_completion_tokens"] = u.completion_tokens if u else 0
    fields["model_calls"] = sum(u.calls for u in usage)
    fields["cost_usd"] = cost
    return fields
