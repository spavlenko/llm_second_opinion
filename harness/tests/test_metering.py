"""The metering proxy against a scripted upstream; see test_pi_docker.py for pi through it."""

import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import pytest
from test_runner import FakeRuntime

from llm_second_opinion.adapters import ADAPTERS
from llm_second_opinion.adapters.base import Layer
from llm_second_opinion.adapters.pi import PiAdapter
from llm_second_opinion.config import AgentSpec, Experiment, Price
from llm_second_opinion.contracts import (
    AgentInfo,
    AgentResult,
    ExitReason,
    ModelEndpoint,
    RoleUsage,
    RunConfig,
    RunIdentity,
)
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.metering import (
    MeteringError,
    MeteringProxy,
    PreflightError,
    StreamUsage,
    Tokens,
    cost_usd,
    force_stream_usage,
    preflight,
    read_usage,
    rewrite,
    summarize_usage,
    usage_tokens,
)
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.report import format_spend, format_table, spend_summary, summarize
from llm_second_opinion.runner import Runner

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SECRET = "sk-test-not-a-real-key"
USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "prompt_tokens_details": {"cached_tokens": 100},
    "completion_tokens_details": {"reasoning_tokens": 20},
}


# --- usage parsing ---------------------------------------------------------------------


def test_usage_from_a_chat_completion_with_details():
    assert usage_tokens({"choices": [], "usage": USAGE}) == Tokens(120, 30, 100, 20)


def test_usage_variants():
    moonshot = {"usage": {"prompt_tokens": 5, "completion_tokens": 1, "cached_tokens": 4}}
    assert usage_tokens(moonshot) == Tokens(5, 1, 4, 0)
    responses = {
        "type": "response.completed",
        "response": {"usage": {"input_tokens": 7, "output_tokens": 2}},
    }
    assert usage_tokens(responses) == Tokens(7, 2, 0, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"usage": None},
        {"usage": {"prompt_tokens": 3}},  # no completion count: not usable
        {"usage": {"prompt_tokens": True, "completion_tokens": 1}},
        "not a dict",
    ],
)
def test_no_usage(payload):
    assert usage_tokens(payload) is None


def sse(*chunks, done=True):
    lines = [f"data: {json.dumps(c)}\n\n".encode() for c in chunks]
    return lines + ([b"data: [DONE]\n\n"] if done else [])


def test_stream_usage_comes_from_the_final_chunk():
    stream = StreamUsage()
    for chunk in sse(
        {"choices": [{"delta": {"content": "hi"}}], "usage": None},
        {"choices": [], "usage": USAGE},
    ):
        for line in chunk.splitlines(keepends=True):
            stream.feed(line)
    assert stream.tokens == Tokens(120, 30, 100, 20)


def test_stream_without_usage():
    stream = StreamUsage()
    for line in [b": keep-alive\n", b"data: {broken\n", *sse({"choices": []})]:
        stream.feed(line)
    assert stream.tokens is None


def test_streaming_requests_are_made_to_include_usage():
    body = {"model": "m", "stream": True, "stream_options": {"x": 1, "include_usage": False}}
    assert force_stream_usage(body)["stream_options"] == {"x": 1, "include_usage": True}
    plain = {"model": "m"}
    assert force_stream_usage(plain) is plain


# --- the proxy against a scripted upstream ---------------------------------------------


class Upstream(ThreadingHTTPServer):
    """Answers each request with the next scripted reply and remembers what it got."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.replies: list[tuple[int, str, list[bytes]]] = []
        self.requests: list[dict] = []
        self.gap_s = 0.0  # pause between streamed chunks

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def reply_json(self, payload: dict, status: int = 200) -> None:
        self.replies.append((status, "application/json", [json.dumps(payload).encode()]))

    def reply_stream(self, chunks: list[bytes]) -> None:
        self.replies.append((200, "text/event-stream", chunks))


class _UpstreamHandler(BaseHTTPRequestHandler):
    server: Upstream

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        status, kind, chunks = self.server.replies.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", kind)
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()
            time.sleep(self.server.gap_s)


@pytest.fixture
def upstream():
    server = Upstream()
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy():
    env = {"LSO_TEST_KEY": SECRET, "LSO_TEST_HEADER": "header-secret"}
    server = MeteringProxy(env=env).start()
    yield server
    server.stop()


def endpoint(upstream, **extra):
    return ModelEndpoint(base_url=upstream.url, model="real-model", **extra)


def post(url, body, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with OPENER.open(req, timeout=10) as resp:
        return resp.read()


def chat_url(proxy, meter, role="executor"):
    return proxy.url(meter, role, "127.0.0.1") + "/chat/completions"


def test_a_rate_limit_is_waited_out_and_both_calls_recorded(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    upstream.reply_json({"error": {"message": "quota window", "type": "rate_limit"}}, status=429)
    upstream.reply_json({"choices": [], "usage": USAGE})
    start = time.monotonic()
    post(chat_url(proxy, meter), {"model": "m", "messages": []})  # the agent sees only the 200
    assert time.monotonic() - start >= 1  # Retry-After: 1
    meter.close(1)
    statuses = [r.status for r in read_usage(tmp_path / "usage.jsonl")]
    assert statuses == [429, 200] and len(upstream.requests) == 2


def test_an_empty_balance_is_not_waited_out(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    message = "Your account is suspended due to insufficient balance, please recharge"
    upstream.reply_json({"error": {"message": message, "type": "quota"}}, status=429)
    start = time.monotonic()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(chat_url(proxy, meter), {"model": "m", "messages": []})
    assert e.value.code == 429 and time.monotonic() - start < 1
    meter.close(1)
    assert [r.status for r in read_usage(tmp_path / "usage.jsonl")] == [429]


def test_routes_to_the_role_endpoint_and_records_usage(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    upstream.reply_json({"choices": [], "usage": USAGE})
    post(chat_url(proxy, meter) + "?x=1", {"model": "as-sent", "messages": []})
    meter.close(1)
    assert upstream.requests[0]["path"] == "/v1/chat/completions?x=1"
    [record] = read_usage(tmp_path / "usage.jsonl")
    assert (record.seq, record.role, record.model, record.status) == (0, "executor", "as-sent", 200)
    counts = (record.prompt_tokens, record.completion_tokens, record.cached_tokens)
    assert counts + (record.reasoning_tokens,) == (120, 30, 100, 20)


def test_unknown_items_and_roles_are_404(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    for url in (
        chat_url(proxy, meter, "advisor"),  # this item has no advisor
        chat_url(proxy, meter).replace(meter.id, "0" * 16),
        f"http://127.0.0.1:{proxy.port}/v1/chat/completions",
    ):
        with pytest.raises(urllib.error.HTTPError) as err:
            post(url, {"model": "m"})
        assert err.value.code == 404
    assert upstream.requests == []


def test_the_proxy_adds_the_secrets_and_drops_the_clients_key(proxy, upstream, tmp_path):
    model = endpoint(
        upstream,
        api_key_env="LSO_TEST_KEY",
        headers={"X-Plain": "plain"},
        header_env={"X-Secret": "LSO_TEST_HEADER"},
    )
    meter = proxy.register({"advisor": model}, tmp_path / "usage.jsonl", None)
    upstream.reply_json({"choices": [], "usage": USAGE})
    post(chat_url(proxy, meter, "advisor"), {"model": "m"}, {"Authorization": "Bearer none"})
    headers = upstream.requests[0]["headers"]
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert (headers["X-Plain"], headers["X-Secret"]) == ("plain", "header-secret")


def test_a_stream_passes_through_unchanged_with_usage_forced(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    chunks = sse(
        {"choices": [{"delta": {"content": "hel"}}], "usage": None},
        {"choices": [{"delta": {"content": "lo"}}], "usage": None},
        {"choices": [], "usage": USAGE},
    )
    upstream.reply_stream(chunks)
    received = post(chat_url(proxy, meter), {"model": "m", "stream": True})
    meter.close(1)
    assert received == b"".join(chunks)
    sent = json.loads(upstream.requests[0]["body"])
    assert sent["stream_options"] == {"include_usage": True}
    assert read_usage(tmp_path / "usage.jsonl")[0].prompt_tokens == 120


def test_a_call_without_usage_fails_the_item(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    upstream.reply_stream(sse({"choices": [{"delta": {"content": "hi"}}]}))
    post(chat_url(proxy, meter), {"model": "m", "stream": True})
    with pytest.raises(MeteringError, match="without usage"):
        meter.close(1)
    assert read_usage(tmp_path / "usage.jsonl") == []


def test_an_error_reply_is_recorded_with_zero_tokens(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None, 3)
    upstream.reply_json({"error": {"message": "overloaded"}}, status=503)
    with pytest.raises(urllib.error.HTTPError) as err:
        post(chat_url(proxy, meter), {"model": "m"})
    assert err.value.code == 503
    meter.close(1)  # nothing billed, nothing missing
    [record] = read_usage(tmp_path / "usage.jsonl")
    assert (record.status, record.prompt_tokens, record.attempt) == (503, 0, 3)
    assert (meter.calls, meter.failed) == (0, 1)
    assert summarize_usage([record]) == []  # not a call that counts


def test_a_connection_failure_is_recorded_as_status_0(proxy, tmp_path):
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    down = ModelEndpoint(base_url=f"http://127.0.0.1:{port}/v1", model="m")
    meter = proxy.register({"executor": down}, tmp_path / "usage.jsonl", None)
    with pytest.raises(urllib.error.HTTPError) as err:
        post(chat_url(proxy, meter), {"model": "m"})
    assert err.value.code == 502
    meter.close(1)
    [record] = read_usage(tmp_path / "usage.jsonl")
    assert (record.status, record.completion_tokens) == (0, 0)
    assert record.latency_ms >= 0


def test_requests_are_recorded_without_messages_or_secrets(proxy, upstream, tmp_path):
    model = endpoint(upstream, api_key_env="LSO_TEST_KEY", header_env={"X-S": "LSO_TEST_HEADER"})
    meter = proxy.register(
        {"executor": model, "advisor": model}, tmp_path / "usage.jsonl", None, attempt=2
    )
    upstream.reply_json({"choices": [], "usage": USAGE})
    upstream.reply_json({"choices": [], "usage": USAGE})
    upstream.reply_json({"choices": [], "usage": USAGE})
    tool = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}
    body = {
        "model": "m", "temperature": 0.2, "top_p": 0.9, "seed": 7, "max_tokens": 100,
        "reasoning_effort": "high", "tools": [tool],
        "messages": [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": "hi"}],
    }  # fmt: skip
    client_key = {"Authorization": "Bearer sk-client-not-forwarded"}
    post(chat_url(proxy, meter), body, client_key)
    post(chat_url(proxy, meter), body | {"messages": []}, client_key)
    post(chat_url(proxy, meter, "advisor"), {"model": "k"}, {"X-LSO-Request-Id": "r1"})
    meter.close(1)
    first, second, advisor = [
        json.loads(line) for line in meter.requests_path.read_text().splitlines()
    ]
    assert first | {"ts": 0} == {
        "seq": 0, "ts": 0, "role": "executor", "attempt": 2, "request_id": None,
        "path": "/chat/completions", "model": "m", "temperature": 0.2, "top_p": 0.9, "seed": 7,
        "max_tokens": 100, "reasoning_effort": "high", "tools": ["bash"], "messages_count": 2,
    }  # fmt: skip
    assert second["messages_count"] == 0 and advisor["request_id"] == "r1"
    full = json.loads((tmp_path / "executor-request.json").read_text())
    assert full["messages"][0]["content"] == "SYSTEM PROMPT" and full["tools"] == [tool]
    usage = read_usage(tmp_path / "usage.jsonl")
    assert [r.request_id for r in usage] == [None, None, "r1"]
    assert {r.attempt for r in usage} == {2}
    # The plugin's request id stays on the host.
    assert all("X-LSO-Request-Id" not in r["headers"] for r in upstream.requests)
    for path in tmp_path.iterdir():
        text = path.read_text()
        for secret in (SECRET, "header-secret", "sk-client-not-forwarded"):
            assert secret not in text, (path.name, secret)


def test_usage_is_read_to_the_end_after_the_client_hangs_up(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", None)
    upstream.gap_s = 0.1
    upstream.reply_stream(
        sse(*[{"choices": [], "usage": None}] * 5, {"choices": [], "usage": USAGE})
    )
    path = urlsplit(chat_url(proxy, meter)).path
    body = json.dumps({"model": "m", "stream": True}).encode()
    with socket.create_connection(("127.0.0.1", proxy.port)) as client:
        client.sendall(
            f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client.recv(64)  # the agent is aborted after the first bytes
    meter.close(5)
    assert read_usage(tmp_path / "usage.jsonl")[0].completion_tokens == 30


def test_budget_refuses_calls_once_spent(proxy, upstream, tmp_path):
    meter = proxy.register({"executor": endpoint(upstream)}, tmp_path / "usage.jsonl", 100)
    upstream.reply_json({"choices": [], "usage": USAGE})  # 150 tokens: over budget after it
    post(chat_url(proxy, meter), {"model": "m"})
    with pytest.raises(urllib.error.HTTPError) as err:
        post(chat_url(proxy, meter), {"model": "m"})
    assert err.value.code == 403
    assert json.loads(err.value.read())["error"]["type"] == "token_limit"
    assert (meter.tokens, meter.refused, len(upstream.requests)) == (150, 1, 1)


# --- preflight -----------------------------------------------------------------------------


def test_preflight_passes_when_streams_report_usage(proxy, upstream, tmp_path):
    upstream.reply_stream(sse({"choices": [], "usage": USAGE}))
    preflight(proxy, {"local": endpoint(upstream)}, tmp_path)
    assert json.loads(upstream.requests[0]["body"])["stream_options"]["include_usage"]
    assert len(read_usage(tmp_path / "local/usage.jsonl")) == 1


def test_preflight_refuses_an_endpoint_without_usage(proxy, upstream, tmp_path):
    upstream.reply_stream(sse({"choices": [{"delta": {"content": "OK"}}]}))
    upstream.reply_json({"error": {"message": "bad key"}}, status=401)
    with pytest.raises(PreflightError) as err:
        preflight(proxy, {"a": endpoint(upstream), "b": endpoint(upstream)}, tmp_path)
    assert "a (real-model): " in str(err.value) and "without usage" in str(err.value)
    assert "b (real-model): HTTP 401" in str(err.value)


def test_mock_server_answers_preflight_without_using_a_recording(proxy, tmp_path):
    recordings = tmp_path / "r.jsonl"
    recordings.write_text(json.dumps({"message": {"content": "real"}}) + "\n")
    mock = MockServer(("127.0.0.1", 0), recordings)
    threading.Thread(target=mock.serve_forever, args=(0.05,), daemon=True).start()
    try:
        preflight(proxy, {"mock": endpoint(mock)}, tmp_path)
        assert mock.served == 0
        meter = proxy.register({"executor": endpoint(mock)}, tmp_path / "u.jsonl", None)
        reply = json.loads(post(chat_url(proxy, meter), {"model": "m", "messages": []}))
        assert reply["usage"]["mock_estimate"] is True  # the recording has no usage
        meter.close(1)
        assert read_usage(tmp_path / "u.jsonl")[0].completion_tokens > 0
    finally:
        mock.shutdown()
        mock.server_close()


# --- cost --------------------------------------------------------------------------------


def role_usage(role, prompt, completion, cached=0):
    return RoleUsage(
        role=role,
        calls=1,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        reasoning_tokens=0,
    )


def test_cost_uses_the_cached_price_and_needs_every_price():
    usage = [role_usage("executor", 1_000_000, 0, cached=400_000), role_usage("advisor", 0, 10)]
    prices = {
        "executor": Price(input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.1),
        "advisor": Price(input_per_mtok=3.0, output_per_mtok=100_000.0),
    }
    assert cost_usd(usage, prices) == pytest.approx(0.6 + 0.04 + 1.0)
    assert cost_usd(usage, {"executor": prices["executor"], "advisor": None}) is None
    assert cost_usd([], {"executor": None}) == 0.0  # no calls cost nothing


# --- runner -----------------------------------------------------------------------------


class ModelAgent:
    """Makes the scripted model calls through whatever endpoint the run config names."""

    name = "model-agent"
    version = "0"
    capabilities = frozenset({"advisor"})
    uses_models = True
    artifacts = ()

    def __init__(self, calls=1):
        self.calls = calls
        self.env_seen = None
        self.configs = []

    def build_layer(self, task_image):
        return Layer(task_image)

    def spans(self, item_dir):
        return []

    def run(self, box, task, config, limits, env):
        self.env_seen = env
        self.configs.append(config)
        # The container's host-gateway name is the host's loopback here.
        parts = urlsplit(config.executor.base_url)
        base = urlunsplit(parts._replace(netloc=f"127.0.0.1:{parts.port}"))
        crashed = False
        for _ in range(self.calls):
            try:
                post(f"{base}/chat/completions", {"model": config.executor.model})
            except urllib.error.HTTPError:
                crashed = True
                break
        return AgentResult(
            diff="",
            exit_reason=ExitReason.CRASH if crashed else ExitReason.FINISHED,
            agent=AgentInfo(name=self.name, version=self.version),
            turns=1,
            duration_s=0.1,
            detail="model error" if crashed else None,
        )


def metered_experiment(repo, upstream, **overrides):
    raw = {
        "name": "m",
        "tasks": repo / "tasks/manifests/toy-v1.yaml",
        "task_ids": ["toy-add"],
        "seeds": 1,
        "limits": {"wall_minutes": 1, "max_turns": 5},
        "execution": {"retries": 0},
        "models": {
            "local": {
                "base_url": upstream.url,
                "model": "real-model",
                "api_key_env": "LSO_TEST_KEY",
                "header_env": {"X-Secret": "LSO_TEST_HEADER"},
            }
        },
        "prices": {"local": {"input_per_mtok": 1.0, "output_per_mtok": 10.0}},
        "arms": [{"name": "A", "agent": "model-agent", "executor": "local"}],
    }
    return Experiment.model_validate(raw | overrides)


@pytest.fixture
def model_agent(monkeypatch):
    agent = ModelAgent()
    monkeypatch.setitem(ADAPTERS, "model-agent", lambda spec: agent)
    monkeypatch.setenv("LSO_TEST_KEY", SECRET)
    monkeypatch.setenv("LSO_TEST_HEADER", "header-secret")
    return agent


def run_metered(exp, tmp_path, preflight=False, tracker=None):
    runner = Runner(
        exp,
        tmp_path,
        runtime=FakeRuntime(),
        echo=lambda _: None,
        preflight=preflight,
        proxy_host="127.0.0.1",
    )
    runner.tracker = tracker
    [outcome] = runner.run()
    [item] = sorted((tmp_path / "m/A/toy-add/seed-0").glob("*/attempt-*"))[-1:]
    return outcome, runner.ledger.rows(exp.name)[0], item


class RecordingTracker:
    def __init__(self):
        self.items, self.arms = [], []

    def log_item(self, key, arm_params, params, metrics, artifacts, trace=None):
        self.items.append((metrics, trace))
        return "run-id"

    def log_arm_summary(self, arm, config_hash, arm_params, metrics):
        self.arms.append(metrics)

    def close(self):
        pass


def test_tokens_and_cost_go_to_mlflow_and_the_trace(repo, tmp_path, upstream, model_agent):
    upstream.reply_json({"choices": [], "usage": USAGE})
    tracker = RecordingTracker()
    run_metered(metered_experiment(repo, upstream), tmp_path, tracker=tracker)
    [(metrics, trace)] = tracker.items
    assert metrics["executor_prompt_tokens"] == 120 and metrics["model_calls"] == 1
    assert metrics["cost_usd"] == pytest.approx(420e-6)
    agent = trace.children[0]
    assert agent.attributes["proxy.executor.prompt_tokens"] == 120
    assert agent.attributes["proxy.executor.cached_tokens"] == 100
    [arm] = tracker.arms
    assert (arm["tokens_sum"], arm["tokens_median"]) == (150, 150)
    assert arm["cost_usd_sum"] == pytest.approx(420e-6)
    assert "cost_usd_per_resolved" not in arm  # an empty diff resolves nothing


def test_runner_meters_an_item_and_the_agent_holds_no_secrets(
    repo, tmp_path, upstream, model_agent
):
    upstream.reply_stream(sse({"choices": [], "usage": USAGE}))  # preflight
    upstream.reply_json({"choices": [], "usage": USAGE})
    exp = metered_experiment(repo, upstream)
    outcome, row, item = run_metered(exp, tmp_path, preflight=True)
    assert outcome.status == "done"
    assert model_agent.env_seen == {}
    advisor_json = (item / "advisor.json").read_text()
    config = model_agent.configs[0]
    assert config.executor.base_url.startswith("http://host.docker.internal:")
    assert "/items/" in config.executor.base_url and upstream.url not in advisor_json
    assert config.executor.api_key_env is None and config.executor.header_env == {}
    assert config.run.config_hash == exp.config_hash(exp.arms[0])
    models_json = json.dumps(PiAdapter(AgentSpec(adapter="pi")).models_json(config.executor))
    for text in (advisor_json, models_json):
        assert SECRET not in text and "header-secret" not in text and "LSO_TEST" not in text
    assert upstream.requests[1]["headers"]["Authorization"] == f"Bearer {SECRET}"

    assert len(read_usage(item / "usage.jsonl")) == 1
    result = json.loads((item / "result.json").read_text())
    assert result["usage"][0]["prompt_tokens"] == 120
    assert (row["executor_prompt_tokens"], row["executor_completion_tokens"]) == (120, 30)
    assert (row["advisor_prompt_tokens"], row["model_calls"]) == (0, 1)
    assert row["cost_usd"] == pytest.approx((120 * 1.0 + 30 * 10.0) / 1e6)


def test_missing_usage_fails_the_item(repo, tmp_path, upstream, model_agent):
    upstream.reply_json({"choices": []})
    outcome, row, _ = run_metered(metered_experiment(repo, upstream), tmp_path)
    assert outcome.status == "failed" and row["status"] == "failed"
    assert "without usage" in row["error"]


def test_preflight_failure_stops_the_batch(repo, tmp_path, upstream, model_agent):
    upstream.reply_stream(sse({"choices": []}))
    runner = Runner(metered_experiment(repo, upstream), tmp_path, runtime=FakeRuntime(),
                    echo=lambda _: None, proxy_host="127.0.0.1")  # fmt: skip
    with pytest.raises(PreflightError):
        runner.run()
    assert runner.ledger.rows("m") == [] and runner.proxy is None


def test_token_budget_ends_the_item_as_token_limit(repo, tmp_path, upstream, model_agent):
    model_agent.calls = 3
    upstream.reply_json({"choices": [], "usage": USAGE})
    limits = {"wall_minutes": 1, "max_turns": 5, "max_tokens": 100}
    outcome, row, item = run_metered(metered_experiment(repo, upstream, limits=limits), tmp_path)
    assert (outcome.status, row["exit_reason"]) == ("done", "token_limit")
    assert len(upstream.requests) == 1 and row["model_calls"] == 1
    assert json.loads((item / "result.json").read_text())["detail"] is None


def test_cost_is_null_without_a_price_and_the_report_shows_tokens(
    repo, tmp_path, upstream, model_agent
):
    upstream.reply_json({"choices": [], "usage": USAGE})
    exp = metered_experiment(repo, upstream, prices={})
    _, row, _ = run_metered(exp, tmp_path)
    assert row["cost_usd"] is None and row["model_calls"] == 1
    [summary] = summarize(exp, [row], 1)
    assert summary.tokens == [150] and summary.cost is None
    assert "0.1" in format_table([summary])  # 150 tokens per item, in thousands


def test_report_cost_per_resolved_task(repo, upstream):
    exp = metered_experiment(repo, upstream)
    key = {"arm": "A", "config_hash": exp.config_hash(exp.arms[0]), "status": "done"}
    tokens = dict.fromkeys(
        ["executor_completion_tokens", "advisor_prompt_tokens", "advisor_completion_tokens"], 0
    )
    rows = [
        key | tokens | {"resolved": r, "duration_s": 60, "turns": 1, "exit_reason": "finished",
                        "executor_prompt_tokens": 1000, "model_calls": 1, "cost_usd": c}
        for r, c in [(1, 0.25), (0, 0.5), (1, 0.75)]
    ]  # fmt: skip
    [s] = summarize(exp, rows, 3)
    assert (s.cost, s.cost_per_resolved, s.tokens) == (1.5, 0.75, [1000] * 3)
    assert "1.50" in format_table([s]) and "0.750" in format_table([s])


# --- ledger and config ---------------------------------------------------------------------


def test_an_old_ledger_gains_the_token_columns(tmp_path):
    path = tmp_path / "ledger.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE items (experiment TEXT NOT NULL, arm TEXT NOT NULL, task TEXT NOT NULL, "
        "seed INTEGER NOT NULL, config_hash TEXT NOT NULL, status TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, resolved INTEGER, grade TEXT, exit_reason TEXT, "
        "turns INTEGER, duration_s REAL, error TEXT, mlflow_run_id TEXT, updated REAL NOT NULL, "
        "PRIMARY KEY (experiment, arm, task, seed, config_hash))"
    )
    db.execute("INSERT INTO items VALUES ('e','a','t',0,'h','done',1,1,'ok','finished',2,1.0,"
               "NULL,NULL,0)")  # fmt: skip
    db.commit()
    db.close()
    ledger = Ledger(path)
    [row] = ledger.rows("e")
    assert row["status"] == "done" and row["cost_usd"] is None and row["model_calls"] is None
    key = ItemKey("e", "a", "t", 1, "h")
    ledger.start(key)
    ledger.finish(key, "done", model_calls=3, cost_usd=0.5, executor_prompt_tokens=10)
    assert Ledger(path).rows("e")[1]["model_calls"] == 3  # reopening migrates nothing twice


def test_max_tokens_is_hashed_only_when_set(repo, upstream):
    exp = metered_experiment(repo, upstream)
    base = exp.config_hash(exp.arms[0])
    with_budget = exp.model_copy(update={"limits": exp.limits.model_copy(update={"max_tokens": 9})})
    assert with_budget.config_hash(exp.arms[0]) != base
    unpriced = exp.model_copy(update={"prices": {}})
    assert unpriced.config_hash(exp.arms[0]) == base


def test_existing_hashes_are_unchanged(repo):
    # Pinned on 2026-10-03, when the hash gained the manifest version and the adapter's
    # fingerprint (here without one, as for the gold adapter) and dropped unset model fields.
    exp = Experiment.from_yaml(repo / "experiments/toy-pi.yaml")
    assert exp.config_hash(exp.arms[0]) == "54ecd6d407a42959"
    fingerprint = {"prompt": "p", "bundle_image": "sha256:1"}
    assert exp.config_hash(exp.arms[0], fingerprint) == "86f4ea1c7ea51290"
    other = exp.config_hash(exp.arms[0], fingerprint | {"bundle_image": "sha256:2"})
    assert other != exp.config_hash(exp.arms[0], fingerprint)


def test_hash_inputs(repo, upstream, tmp_path):
    exp = metered_experiment(repo, upstream)
    arm = exp.arms[0]
    base = exp.config_hash(arm)
    sampled = exp.model_copy(
        update={"models": {"local": exp.models["local"].model_copy(update={"temperature": 0.0})}}
    )
    assert sampled.config_hash(arm) != base  # sampling changes behaviour
    assert exp.config_hash(arm, {"prompt": "a"}) != exp.config_hash(arm, {"prompt": "b"})
    manifest = tmp_path / "toy-v9.yaml"
    manifest.write_text(
        (repo / "tasks/manifests/toy-v1.yaml").read_text().replace("toy-v1", "toy-v9")
    )
    assert metered_experiment(repo, upstream, tasks=manifest).config_hash(arm) != base


def test_retry_keeps_both_attempts_and_both_spends(repo, tmp_path, upstream, model_agent):
    class FailsOnce(ModelAgent):
        def run(self, box, task, config, limits, env):
            result = super().run(box, task, config, limits, env)
            if len(self.configs) == 1:
                raise RuntimeError("container died after a model call")
            return result

    agent = FailsOnce()
    ADAPTERS["model-agent"] = lambda spec: agent
    upstream.reply_json({"choices": [], "usage": USAGE})
    upstream.reply_json({"choices": [], "usage": USAGE})
    exp = metered_experiment(repo, upstream, execution={"retries": 1})
    outcome, row, item = run_metered(exp, tmp_path)
    assert outcome.status == "done" and item.name == "attempt-2"
    runner = Runner(
        exp, tmp_path, runtime=FakeRuntime(), echo=lambda _: None, proxy_host="127.0.0.1"
    )
    attempts = runner.ledger.attempts("m")
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "failed"), (2, "done")]
    assert [a["executor_prompt_tokens"] for a in attempts] == [120, 120]
    assert attempts[0]["cost_usd"] == attempts[1]["cost_usd"] == pytest.approx(420e-6)
    # Each attempt has its own usage record; the item counts only the second.
    first = item.parent / "attempt-1"
    assert [r.attempt for r in read_usage(first / "usage.jsonl")] == [1]
    assert [r.attempt for r in read_usage(item / "usage.jsonl")] == [2]
    assert row["executor_prompt_tokens"] == 120 and row["attempt_dir"].endswith("attempt-2")
    spent = spend_summary(exp, [row], attempts, runner.ledger.preflight("m"))
    assert spent.counted.cost_usd == pytest.approx(420e-6)
    assert spent.attempts.cost_usd == pytest.approx(840e-6)
    assert spent.total.cost_usd == pytest.approx(840e-6)  # no preflight in this test
    assert "all attempts" in format_spend(spent)


def test_prices_must_name_models(repo, upstream):
    with pytest.raises(ValueError, match="prices for models not in models: nope"):
        metered_experiment(
            repo, upstream, prices={"nope": {"input_per_mtok": 1, "output_per_mtok": 1}}
        )


def test_rewrite_keeps_model_settings_but_not_routing(proxy, upstream, tmp_path):
    model = endpoint(upstream, reasoning_effort="high", api_key_env="K", headers={"X": "1"})
    config = RunConfig(
        run=RunIdentity(experiment="e", arm="a", task="t", seed=0, config_hash="h"),
        executor=model,
        advisor_model=model,
    )
    meter = proxy.register({"executor": model, "advisor": model}, tmp_path / "u.jsonl", None)
    routed = rewrite(config, proxy, meter)
    assert routed.executor.reasoning_effort == "high" and routed.executor.headers == {}
    assert routed.advisor_model.base_url.endswith(f"/items/{meter.id}/advisor/v1")
    assert routed.run == config.run
