"""Runner behaviour with an in-memory runtime and agent; see test_docker.py for real containers."""

import threading

import pytest

from llm_second_opinion.adapters import ADAPTERS
from llm_second_opinion.adapters.base import Layer
from llm_second_opinion.config import ConfigError, Experiment
from llm_second_opinion.contracts import AgentInfo, AgentResult, ExitReason
from llm_second_opinion.runner import Runner
from llm_second_opinion.runtime import ExecResult


class FakeContainer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.files = {}

    def exec(self, command, workdir=None, timeout_s=None, env=None):
        return ExecResult(0, "")

    def write(self, path, data):
        self.files[path] = data

    def read(self, path):
        return self.files.get(path)

    def remove(self):
        self.runtime.live -= 1


class FakeRuntime:
    def __init__(self):
        self.started = 0
        self.live = 0

    def start(self, image, cpus, memory_gb, name, volumes=None):
        self.started += 1
        self.live += 1
        return FakeContainer(self)


class FakeAgent:
    name = "fake"
    version = "0"
    capabilities = frozenset()
    artifacts = ()

    def __init__(self, fail_times=0, barrier=None):
        self.fail_times = fail_times
        self.barrier = barrier
        self.env_seen = None

    def build_layer(self, task_image):
        return Layer(task_image)

    def spans(self, item_dir):
        return []

    def run(self, box, task, config, limits, env):
        self.env_seen = env
        if self.barrier:
            self.barrier.wait()
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("docker went away")
        box.write(config.events_path, '{"seq": 0}\n')
        return AgentResult(
            diff="--- a\n+++ b\n",
            exit_reason=ExitReason.FINISHED,
            agent=AgentInfo(name="fake", version="0"),
            turns=2,
            duration_s=1.5,
        )


def experiment(repo, **overrides):
    raw = {
        "name": "t",
        "tasks": repo / "tasks/manifests/toy-v1.yaml",
        "seeds": 2,
        "limits": {"wall_minutes": 1, "max_turns": 5},
        "models": {"m": {"base_url": "http://x/v1", "model": "m"}},
        "arms": [{"name": "F", "agent": "fake", "executor": "m"}],
    }
    return Experiment.model_validate({**raw, **overrides})


@pytest.fixture
def agent(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setitem(ADAPTERS, "fake", lambda spec: agent)
    return agent


def make_runner(repo, tmp_path, **kwargs):
    exp = kwargs.pop("exp", None) or experiment(repo)
    return Runner(exp, tmp_path, runtime=FakeRuntime(), echo=lambda _: None, **kwargs)


def test_every_item_runs_is_graded_and_leaves_artifacts(repo, tmp_path, agent):
    runner = make_runner(repo, tmp_path)
    outcomes = runner.run()
    assert [o.status for o in outcomes] == ["done"] * 4
    assert all(o.resolved for o in outcomes)
    assert runner.runtime.started == 8  # agent + grading container per item
    assert runner.runtime.live == 0
    item = tmp_path / "t/F/toy-add/seed-1"
    assert {p.name for p in item.iterdir()} == {
        "advisor.json", "result.json", "patch.diff", "events.jsonl", "grade.log"
    }  # fmt: skip
    assert (item / "events.jsonl").read_text() == '{"seq": 0}\n'


def test_items_are_seed_major(repo, tmp_path, agent):
    keys = [str(i.key) for i in make_runner(repo, tmp_path).items()]
    assert keys == [
        "F/toy-add/seed-0", "F/toy-max/seed-0", "F/toy-add/seed-1", "F/toy-max/seed-1"
    ]  # fmt: skip


def test_resume_skips_done_items(repo, tmp_path, agent):
    make_runner(repo, tmp_path).run()
    runner = make_runner(repo, tmp_path)
    assert [o.status for o in runner.run()] == ["skipped"] * 4
    assert runner.runtime.started == 0


def test_infrastructure_error_is_retried(repo, tmp_path, agent):
    agent.fail_times = 1
    runner = make_runner(repo, tmp_path)
    assert [o.status for o in runner.run(task="toy-add")] == ["done", "done"]
    attempts = sorted(r["attempts"] for r in runner.ledger.rows("t"))
    assert attempts == [1, 2]


def test_item_fails_after_retries_without_stopping_the_batch(repo, tmp_path, agent):
    agent.fail_times = 2  # retries=1: the first item uses both attempts
    exp = experiment(repo, execution={"parallel": 1, "retries": 1})
    runner = make_runner(repo, tmp_path, exp=exp)
    statuses = [o.status for o in runner.run()]
    assert statuses == ["failed", "done", "done", "done"]
    failed = [r for r in runner.ledger.rows("t") if r["status"] == "failed"]
    assert failed[0]["error"] == "RuntimeError: docker went away"
    assert runner.runtime.live == 0


def test_parallel_items_overlap(repo, tmp_path, agent):
    # Each run waits until two are in flight: this only completes if items run concurrently.
    agent.barrier = threading.Barrier(2, timeout=5)
    runner = make_runner(repo, tmp_path, parallel=2)
    assert [o.status for o in runner.run()] == ["done"] * 4


def test_api_keys_come_from_the_environment(repo, tmp_path, agent, monkeypatch):
    models = {"m": {"base_url": "http://x/v1", "model": "m", "api_key_env": "LSO_TEST_KEY"}}
    exp = experiment(repo, models=models)
    monkeypatch.delenv("LSO_TEST_KEY", raising=False)
    with pytest.raises(ConfigError, match="LSO_TEST_KEY"):
        make_runner(repo, tmp_path, exp=exp).run()
    monkeypatch.setenv("LSO_TEST_KEY", "secret")
    make_runner(repo, tmp_path, exp=exp).run()
    assert agent.env_seen == {"LSO_TEST_KEY": "secret"}
    advisor_json = (tmp_path / "t/F/toy-add/seed-0/advisor.json").read_text()
    assert "secret" not in advisor_json


def test_arm_needing_a_missing_capability_is_refused(repo, tmp_path, agent):
    arm = {"name": "G", "agent": "gold", "executor": "advisor", "advisor": {"level": "L2"}}
    models = {"advisor": {"base_url": "http://x/v1", "model": "m"}}
    exp = experiment(repo, models=models, arms=[arm])
    with pytest.raises(ConfigError, match="does not support an advisor"):
        make_runner(repo, tmp_path, exp=exp)


def test_unknown_agent_is_refused(repo, tmp_path):
    exp = experiment(repo, arms=[{"name": "X", "agent": "nope", "executor": "m"}])
    with pytest.raises(ConfigError, match="unknown agent 'nope'"):
        make_runner(repo, tmp_path, exp=exp)
