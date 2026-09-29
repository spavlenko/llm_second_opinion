"""pi in real containers against the mock model server; skipped when no daemon is reachable.

The first run builds the pi bundle image (npm install), which takes a minute.
"""

import json
import shutil
import subprocess
import threading

import docker
import pytest

from llm_second_opinion.adapters.pi import PiAdapter
from llm_second_opinion.config import AgentSpec, Experiment
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
    """A mock model server on all interfaces, so containers reach it through the host gateway."""

    def start(recordings: list[dict] | None = None) -> str:
        path = tmp_path / "recordings.jsonl"
        if recordings is None:
            shutil.copy(repo / "harness/tests/fixtures/pi-toy-add.jsonl", path)
        else:
            path.write_text("".join(json.dumps(r) + "\n" for r in recordings))
        server = MockServer(("0.0.0.0", 0), path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    servers: list[MockServer] = []
    yield start
    for server in servers:
        server.shutdown()


def run_pi(repo, tmp_path, base_url, **limits):
    exp = Experiment.model_validate(
        {
            "name": "pi",
            "tasks": repo / "tasks/manifests/toy-v1.yaml",
            "seeds": 1,
            "limits": {"wall_minutes": 5, "max_turns": 10} | limits,
            "execution": {"cpus": 1, "memory_gb": 1, "retries": 0},
            "models": {"mock": {"base_url": base_url, "model": "mock"}},
            "arms": [{"name": "pi", "agent": "pi", "executor": "mock"}],
        }
    )
    runner = Runner(exp, tmp_path / "runs", echo=lambda _: None)
    [outcome] = runner.run(task="toy-add")
    item = tmp_path / "runs/pi/pi/toy-add/seed-0"
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
