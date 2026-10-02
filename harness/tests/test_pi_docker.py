"""pi in real containers against the mock model server, through the metering proxy; skipped
when no daemon is reachable.

The first run builds the pi bundle image (npm install), which takes a minute. Both servers
bind 127.0.0.1: containers reach only the proxy, through the host gateway.
"""

import json
import shutil
import subprocess
import threading

import docker
import pytest

from llm_second_opinion.adapters.pi import PiAdapter
from llm_second_opinion.config import AgentSpec, Experiment
from llm_second_opinion.metering import read_usage
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.runner import Runner

try:
    docker.from_env().ping()
except docker.errors.DockerException:
    pytest.skip("no Docker daemon", allow_module_level=True)

pytestmark = pytest.mark.docker

TOY_IMAGE = "llm-second-opinion/toy:1"


@pytest.fixture(scope="module")
def toy_image(repo):
    subprocess.run(["docker", "build", "-q", "-t", TOY_IMAGE, str(repo / "tasks/toy")], check=True)


@pytest.fixture
def mock(repo, tmp_path):
    """A mock model server on loopback; only the metering proxy talks to it."""

    def start(recordings: list[dict] | None = None) -> str:
        path = tmp_path / "recordings.jsonl"
        if recordings is None:
            shutil.copy(repo / "harness/tests/fixtures/pi-toy-add.jsonl", path)
        else:
            path.write_text("".join(json.dumps(r) + "\n" for r in recordings))
        server = MockServer(("127.0.0.1", 0), path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    servers: list[MockServer] = []
    yield start
    for server in servers:
        server.shutdown()


def run_pi(repo, tmp_path, base_url, model=None, **limits):
    exp = Experiment.model_validate(
        {
            "name": "pi",
            "tasks": repo / "tasks/manifests/toy-v1.yaml",
            "seeds": 1,
            "limits": {"wall_minutes": 5, "max_turns": 10} | limits,
            "execution": {"cpus": 1, "memory_gb": 1, "retries": 0},
            "models": {"mock": {"base_url": base_url, "model": "mock"} | (model or {})},
            "prices": {"mock": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            "arms": [{"name": "pi", "agent": "pi", "executor": "mock"}],
        }
    )
    runner = Runner(exp, tmp_path / "runs", echo=lambda _: None)  # with the usage preflight
    [outcome] = runner.run(task="toy-add")
    item = tmp_path / "runs/pi/pi/toy-add/seed-0"
    if outcome.status == "failed":
        raise AssertionError(runner.ledger.rows("pi")[0]["error"])
    outcome.row = runner.ledger.rows("pi")[0]
    return outcome, json.loads((item / "result.json").read_text()), item


def bash_call(command: str) -> dict:
    call = {"name": "bash", "arguments": json.dumps({"command": command})}
    return {
        "message": {
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": call}],
        }
    }


def test_pi_fixes_the_task_and_is_graded(repo, tmp_path, toy_image, mock):
    outcome, result, item = run_pi(repo, tmp_path, mock())
    assert outcome.resolved
    assert (result["exit_reason"], result["turns"]) == ("finished", 2)
    assert {"prompt.md", "pi.jsonl", "pi.stderr", "timeline.jsonl"} <= {
        p.name for p in item.iterdir()
    }
    turns = PiAdapter(AgentSpec(adapter="pi")).spans(item)
    assert [t.name for t in turns] == ["turn 1", "turn 2"]
    model, tool = turns[0].children
    assert (model.kind, model.attributes["tokens.input"]) == ("CHAT_MODEL", 900)
    assert model.outputs["tool_calls"][0]["name"] == "bash"
    assert (tool.kind, tool.outputs) == ("TOOL", "5\n")
    assert turns[0].start_ns <= model.start_ns <= model.end_ns <= tool.start_ns <= tool.end_ns
    assert "`add 2 3` prints -1" in (item / "prompt.md").read_text()  # the issue text


def test_pi_is_metered_through_the_proxy_without_secrets(
    repo, tmp_path, toy_image, mock, monkeypatch
):
    monkeypatch.setenv("LSO_TEST_KEY", "sk-docker-test-not-real")
    model = {"api_key_env": "LSO_TEST_KEY", "header_env": {"X-Secret": "LSO_TEST_KEY"}}
    outcome, result, item = run_pi(repo, tmp_path, mock(), model)
    assert outcome.resolved
    records = read_usage(item / "usage.jsonl")
    assert [(r.prompt_tokens, r.completion_tokens) for r in records] == [(900, 40), (1000, 10)]
    assert [r.role for r in records] == ["executor", "executor"]
    assert result["usage"][0] | {"role": "executor"} == {
        "role": "executor", "calls": 2, "prompt_tokens": 1900, "completion_tokens": 50,
        "cached_tokens": 0, "reasoning_tokens": 0,
    }  # fmt: skip
    row = outcome.row
    assert (row["executor_prompt_tokens"], row["model_calls"]) == (1900, 2)
    assert row["cost_usd"] == pytest.approx((1900 * 1.0 + 50 * 2.0) / 1e6)
    models_json = (item / "models.json").read_text()
    assert "/items/" in json.loads(models_json)["providers"]["lso"]["baseUrl"]
    for name in ("advisor.json", "models.json"):
        text = (item / name).read_text()
        assert "sk-docker-test-not-real" not in text and "LSO_TEST_KEY" not in text
    assert "127.0.0.1" not in models_json  # the real endpoint stays on the host


def test_token_budget_stops_pi(repo, tmp_path, toy_image, mock):
    # The first reply uses 940 tokens, so the proxy refuses the second call.
    outcome, result, item = run_pi(repo, tmp_path, mock(), max_tokens=500)
    assert result["exit_reason"] == "token_limit"
    assert len(read_usage(item / "usage.jsonl")) == 1
    assert outcome.resolved  # the fix landed in the first turn


def test_turn_limit_stops_pi(repo, tmp_path, toy_image, mock):
    outcome, result, _ = run_pi(repo, tmp_path, mock(), max_turns=1)
    assert (result["exit_reason"], result["turns"]) == ("turn_limit", 1)
    assert outcome.resolved  # the fix landed in the first turn


def test_model_error_is_a_crash(repo, tmp_path, toy_image, mock):
    outcome, result, _ = run_pi(repo, tmp_path, mock([]))  # every request gets a 404
    assert result["exit_reason"] == "crash" and result["detail"]
    assert not outcome.resolved


def test_wall_clock_limit(repo, tmp_path, toy_image, mock):
    outcome, result, _ = run_pi(repo, tmp_path, mock([bash_call("sleep 120")]), wall_minutes=0.1)
    assert result["exit_reason"] == "time_limit"
    assert not outcome.resolved
