import json
import threading
import urllib.error
import urllib.request

import pytest

from llm_second_opinion.mock_server import MockServer

# Skip the macOS system proxy lookup, which can take tens of seconds.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REQUEST = {"model": "qwen", "messages": [{"role": "user", "content": "fix the bug"}]}


@pytest.fixture
def start():
    servers = []

    def _start(recordings, **kwargs):
        server = MockServer(("127.0.0.1", 0), recordings, **kwargs)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def write_recordings(path, *entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def post(server, body):
    req = urllib.request.Request(
        f"{server.url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with OPENER.open(req) as resp:
        return resp.read().decode()


def test_replays_in_order_then_404(start, tmp_path):
    path = write_recordings(
        tmp_path / "r.jsonl", {"message": {"content": "one"}}, {"message": {"content": "two"}}
    )
    server = start(path)
    first = json.loads(post(server, REQUEST))
    assert first["choices"][0]["message"] == {"role": "assistant", "content": "one"}
    assert first["choices"][0]["finish_reason"] == "stop"
    assert json.loads(post(server, REQUEST))["choices"][0]["message"]["content"] == "two"
    with pytest.raises(urllib.error.HTTPError) as err:
        post(server, REQUEST)
    assert err.value.code == 404


def test_streams_tool_calls(start, tmp_path):
    call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    path = write_recordings(
        tmp_path / "r.jsonl", {"message": {"tool_calls": [call]}, "usage": {"prompt_tokens": 3}}
    )
    raw = post(start(path), {**REQUEST, "stream": True, "stream_options": {"include_usage": True}})
    events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "bash"
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {"prompt_tokens": 3}


def test_records_from_upstream_then_replays(start, tmp_path):
    upstream = start(write_recordings(tmp_path / "up.jsonl", {"message": {"content": "real"}}))
    path = tmp_path / "rec.jsonl"
    recorder = start(path, upstream=upstream.url)
    assert json.loads(post(recorder, REQUEST))["choices"][0]["message"]["content"] == "real"

    replayer = start(path)
    assert json.loads(post(replayer, REQUEST))["choices"][0]["message"]["content"] == "real"
