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
from llm_second_opinion.contracts import parse_events
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


def run_pi(repo, tmp_path, base_url, model=None, advisor=None, fail_ok=False, **limits):
    arm = {"name": "pi", "agent": "pi", "executor": "mock"} | (
        {"advisor": advisor} if advisor else {}
    )
    models = {"mock": {"base_url": base_url, "model": "mock"} | (model or {})}
    if advisor:  # the same mock server answers for the advisor, in recording order
        models["advisor"] = {"base_url": base_url, "model": "mock-advisor"}
    exp = Experiment.model_validate(
        {
            "name": "pi",
            "tasks": repo / "tasks/manifests/toy-v1.yaml",
            "seeds": 1,
            "limits": {"wall_minutes": 5, "max_turns": 10} | limits,
            "execution": {"cpus": 1, "memory_gb": 1, "retries": 0},
            "models": models,
            "prices": {m: {"input_per_mtok": 1.0, "output_per_mtok": 2.0} for m in models},
            "arms": [arm],
        }
    )
    runner = Runner(exp, tmp_path / "runs", echo=lambda _: None)  # with the usage preflight
    [outcome] = runner.run(task="toy-add")
    [item] = (tmp_path / "runs/pi/pi/toy-add/seed-0").glob("*/attempt-1")
    outcome.row = runner.ledger.rows("pi")[0]
    if outcome.status == "failed":
        if fail_ok:
            return outcome, None, item
        raise AssertionError(outcome.row["error"])
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
    model = {"api_key_env": "LSO_TEST_KEY", "header_env": {"X-Secret": "LSO_TEST_KEY"},
             "temperature": 0.3}  # fmt: skip
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
    first = json.loads((item / "executor-request.json").read_text())
    assert first["messages"][0]["role"] == "system" and first["tools"]
    params = [json.loads(line) for line in (item / "requests.jsonl").read_text().splitlines()]
    assert [p["role"] for p in params] == ["executor", "executor"]
    assert "bash" in params[0]["tools"] and params[0]["temperature"] == 0.3
    for name in ("advisor.json", "models.json", "requests.jsonl", "executor-request.json"):
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


def test_a_model_endpoint_failure_is_infrastructure(repo, tmp_path, toy_image, mock):
    # Every request gets a 404: an outage, so the item fails (retries: 0) and is not counted.
    outcome, _, item = run_pi(repo, tmp_path, mock([]), fail_ok=True)
    assert outcome.status == "failed" and "model endpoint failed: 404" in outcome.row["error"]
    assert json.loads((item / "agent-result.json").read_text())["exit_reason"] == "crash"
    assert (item / "pi.jsonl").exists() and (item / "timeline.jsonl").exists()
    [record] = read_usage(item / "usage.jsonl")  # the failed call, with no tokens
    assert (record.status, record.prompt_tokens) == (404, 0)


def test_wall_clock_limit(repo, tmp_path, toy_image, mock):
    outcome, result, _ = run_pi(repo, tmp_path, mock([bash_call("sleep 120")]), wall_minutes=0.1)
    assert result["exit_reason"] == "time_limit"
    assert not outcome.resolved


def test_pi_consults_the_advisor_through_the_plugin(repo, tmp_path, toy_image, mock):
    recordings = repo / "harness/tests/fixtures/pi-toy-add-consult.jsonl"
    base_url = mock([json.loads(line) for line in recordings.read_text().splitlines()])
    advisor = {"level": "L1", "interventions": ["consult"], "max_consults": 2}
    outcome, result, item = run_pi(repo, tmp_path, base_url, advisor=advisor)
    assert outcome.resolved
    assert result["exit_reason"] == "finished"

    events = parse_events((item / "events.jsonl").read_text())  # validates every event
    assert [e.type for e in events] == [
        "policy_rendered",
        "consult_requested", "brief_built", "advisor_request", "advisor_response", "advice_applied",
    ]  # fmt: skip
    policy, requested, brief, request, response, applied = events
    assert policy.prompt_hash == request.prompt_hash and policy.consult_tool
    assert brief.role_map["<function_1>"] == "add_numbers"
    assert requested.reason == "Why does add_numbers() in math.sh print -1 for 2 and 3?"
    assert brief.level == "L1" and brief.identifiers_redacted >= 2
    # L1: identifiers left the container only as placeholders.
    assert "add_numbers" not in request.brief_text and "math.sh" not in request.brief_text
    assert "<function_1>" in request.brief_text
    assert request.prompt_hash == json.loads((item / "advisor.json").read_text())["prompts"]["hash"]
    assert response.output_tokens == 12 and applied.request_id == request.request_id

    # The advice reached pi as the tool result, with the placeholder mapped back.
    [advice] = [json.loads(line) for line in (item / "advice.jsonl").read_text().splitlines()]
    assert "add_numbers subtracts" in advice["injected"]
    pi_events = (item / "pi.jsonl").read_text()
    assert "add_numbers subtracts" in pi_events

    # Both roles were metered; the advisor call is in the trace under the consult tool.
    assert [r.role for r in read_usage(item / "usage.jsonl")] == [
        "executor", "advisor", "executor", "executor",
    ]  # fmt: skip
    turns = PiAdapter(AgentSpec(adapter="pi")).spans(item)
    [tool] = [s for s in turns[0].children if s.name == "consult"]
    [consult] = tool.children
    assert consult.name == "advisor: consult"
    assert [c.name for c in consult.children] == ["brief", "advisor"]
    assert consult.children[1].outputs.startswith("<function_1> subtracts")
