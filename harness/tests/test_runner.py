"""Runner behaviour with an in-memory runtime and agent; see test_docker.py for real containers."""

import json
import threading

import pytest

from llm_second_opinion.adapters import ADAPTERS
from llm_second_opinion.adapters.base import AgentInfraError, Layer
from llm_second_opinion.config import ConfigError, Experiment
from llm_second_opinion.contracts import AgentInfo, AgentResult, ExitReason
from llm_second_opinion.grading import GRADER_VERSION
from llm_second_opinion.runner import Runner
from llm_second_opinion.runtime import ExecResult
from llm_second_opinion.tasks import Manifest

TOY = "llm-second-opinion/toy:1"


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
    uses_models = False  # test_metering.py has an agent that does
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


def attempt_dir(root, item="t/F/toy-add/seed-0", k=1):
    """runs/<exp>/<arm>/<task>/seed-<n>/<config_hash>/attempt-<k>, for the one hash there."""
    [path] = (root / item).glob(f"*/attempt-{k}")
    return path


def test_every_item_runs_is_graded_and_leaves_artifacts(repo, tmp_path, agent):
    runner = make_runner(repo, tmp_path)
    outcomes = runner.run()
    assert [o.status for o in outcomes] == ["done"] * 4
    assert all(o.resolved for o in outcomes)
    assert runner.runtime.started == 8  # agent + grading container per item
    assert runner.runtime.live == 0
    item = attempt_dir(tmp_path, "t/F/toy-add/seed-1")
    assert {p.name for p in item.iterdir()} == {
        "advisor.json", "result.json", "patch.diff", "events.jsonl", "grade.log", "grade.json",
        "item.json", "metrics.json",
    }  # fmt: skip
    assert (item / "events.jsonl").read_text() == '{"seq": 0}\n'
    record = json.loads((item / "item.json").read_text())
    assert (record["attempt"], record["task_image"], record["parallel"]) == (1, TOY, 1)
    assert record["manifest_version"] == "toy-v1" and "commit" in record["harness"]
    assert record["agent_started"] <= record["agent_ended"] <= record["grade_ended"]
    [row] = [r for r in runner.ledger.rows("t") if (r["task"], r["seed"]) == ("toy-add", 1)]
    assert row["attempt_dir"] == str(item.relative_to(tmp_path / "t"))
    assert row["image_id"] == TOY


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
    # Both attempts are kept, each in its own directory; the counted one is recorded.
    rows = [r for r in runner.ledger.attempts("t") if r["seed"] == 0]
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "failed"), (2, "done")]
    assert rows[0]["error"] == "RuntimeError: docker went away"
    first, second = attempt_dir(tmp_path, k=1), attempt_dir(tmp_path, k=2)
    assert (first / "item.json").exists() and not (first / "result.json").exists()
    assert (second / "result.json").exists()
    [item] = [r for r in runner.ledger.rows("t") if r["seed"] == 0]
    assert item["attempt_dir"] == rows[1]["dir"]


def test_attempts_start_empty_and_never_reuse_a_directory(repo, tmp_path, agent):
    runner = make_runner(repo, tmp_path)
    runner.run(task="toy-add")
    stale = attempt_dir(tmp_path)
    (stale / "advice.jsonl").write_text("stale")
    # A new ledger (the old one deleted) still does not reuse attempt-1.
    (tmp_path / "t/ledger.sqlite").unlink()
    make_runner(repo, tmp_path).run(task="toy-add")
    fresh = attempt_dir(tmp_path, k=2)
    assert not (fresh / "advice.jsonl").exists()
    assert (stale / "advice.jsonl").read_text() == "stale"


class FlakyGrading(FakeRuntime):
    """The grading container fails to start the first `fails` times."""

    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    def start(self, image, cpus, memory_gb, name, volumes=None):
        if name.startswith("grade") and self.fails:
            self.fails -= 1
            raise RuntimeError("docker is down")
        return super().start(image, cpus, memory_gb, name, volumes)


def test_failed_grading_is_retried_without_running_the_agent_again(repo, tmp_path, agent):
    calls = []
    run = agent.run
    agent.run = lambda *a: calls.append(1) or run(*a)
    exp = experiment(repo, seeds=1, execution={"retries": 1})
    runner = Runner(exp, tmp_path, runtime=FlakyGrading(1), echo=lambda _: None)
    assert [o.status for o in runner.run(task="toy-add")] == ["done"]
    assert len(calls) == 1
    [attempt] = runner.ledger.attempts("t")
    assert (attempt["attempt"], attempt["status"]) == (1, "done")
    assert "docker is down" in attempt["error"]  # kept: the phase that failed once

    # Retries used up: the item fails, and the next batch only grades the saved result.
    runner = Runner(exp, tmp_path / "b", runtime=FlakyGrading(2), echo=lambda _: None)
    assert [o.status for o in runner.run(task="toy-add")] == ["failed"]
    assert len(calls) == 2
    runner = Runner(exp, tmp_path / "b", runtime=FakeRuntime(), echo=lambda _: None)
    assert [o.status for o in runner.run(task="toy-add")] == ["done"]
    assert len(calls) == 2
    [row] = runner.ledger.rows("t")
    assert (row["status"], row["attempts"]) == ("done", 1)


def test_an_attempt_left_running_is_marked_interrupted(repo, tmp_path, agent):
    runner = make_runner(repo, tmp_path, exp=experiment(repo, seeds=1))
    [item] = runner.items(task="toy-add")
    runner.ledger.start(item.key)
    runner.ledger.new_attempt(item.key, runner.dir)  # a process died here
    assert [o.status for o in runner.run(task="toy-add")] == ["done"]
    statuses = [r["status"] for r in runner.ledger.attempts("t")]
    assert statuses == ["interrupted", "done"]


class Outage(FakeAgent):
    """The model endpoint is down for the first `fail_times` runs (pi raises this)."""

    def run(self, box, task, config, limits, env):
        if self.fail_times:
            self.fail_times -= 1
            result = AgentResult(diff="", exit_reason=ExitReason.CRASH, turns=0, duration_s=1,
                                 agent=AgentInfo(name="fake", version="0"), detail="503")  # fmt: skip
            raise AgentInfraError("model endpoint failed: 503", result)
        return super().run(box, task, config, limits, env)


def test_an_endpoint_outage_is_retried_and_never_a_done_crash(repo, tmp_path, monkeypatch):
    agent = Outage(fail_times=1)
    monkeypatch.setitem(ADAPTERS, "fake", lambda spec: agent)
    exp = experiment(repo, seeds=1, execution={"retries": 1})
    runner = make_runner(repo, tmp_path, exp=exp)
    assert [o.status for o in runner.run(task="toy-add")] == ["done"]
    first = attempt_dir(tmp_path, k=1)
    assert json.loads((first / "agent-result.json").read_text())["detail"] == "503"
    assert not (first / "result.json").exists()

    agent.fail_times = 2
    runner = make_runner(repo, tmp_path / "b", exp=exp)
    assert [o.status for o in runner.run(task="toy-add")] == ["failed"]
    [row] = runner.ledger.rows("t")
    assert row["status"] == "failed" and "model endpoint failed" in row["error"]
    assert row["exit_reason"] is None  # not counted as an unresolved crash


def test_report_columns_and_report_json(repo, tmp_path, agent):
    from click.testing import CliRunner

    from llm_second_opinion.cli import main
    from llm_second_opinion.report import ITEM_COLUMNS

    exp_yaml = tmp_path / "t.yaml"
    exp_yaml.write_text(
        f"name: t\ntasks: {repo / 'tasks/manifests/toy-v1.yaml'}\nseeds: 1\n"
        "limits: {wall_minutes: 1, max_turns: 5}\nmodels: {m: {base_url: 'http://x/v1', model: m}}\n"
        "arms: [{name: F, agent: fake, executor: m}]\n"
    )
    make_runner(repo, tmp_path / "runs", exp=experiment(repo, seeds=1)).run()
    csv_path = tmp_path / "items.csv"
    args = ["report", str(exp_yaml), "--runs-dir", str(tmp_path / "runs"), "--csv", str(csv_path)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "all attempts" in result.output and "preflight" in result.output
    header = csv_path.read_text().splitlines()[0].split(",")
    assert header == ITEM_COLUMNS
    for column in ("executor_cached_tokens", "advisor_reasoning_tokens", "prompt_hash", "level",
                   "interventions", "f2p_passed", "build_failed", "edited_tests", "attempt_dir"):  # fmt: skip
        assert column in header
    record = json.loads((tmp_path / "runs/t/report.json").read_text())
    assert record["arms"][0]["done"] == 2 and record["arms"][0]["attempts"] == 2
    assert set(record) >= {"paired", "pareto", "spend", "variants_tried"}
    assert list((tmp_path / "runs/t/reports").glob("*.json"))


def test_regrade_grades_stored_patches_again(repo, tmp_path, agent):
    runner = make_runner(repo, tmp_path, exp=experiment(repo, seeds=1))
    runner.run(task="toy-add")
    item = attempt_dir(tmp_path)
    old = json.loads((item / "grade.json").read_text())
    (item / "grade.json").write_text(
        json.dumps({k: v for k, v in old.items() if k != "grader_version"})
    )
    runner = make_runner(repo, tmp_path, exp=experiment(repo, seeds=1))
    [outcome] = runner.regrade(task="toy-add")
    assert outcome.status == "done" and runner.runtime.started == 1  # grading only
    assert (item / "grade.v1.json").exists() and (item / "grade.v1.log").exists()
    assert json.loads((item / "grade.json").read_text())["grader_version"] == GRADER_VERSION


def test_infrastructure_error_in_the_agent_saves_its_artifacts(repo, tmp_path, monkeypatch):
    class Leaves(FakeAgent):
        artifacts = ("/run/lso/pi.jsonl",)

        def run(self, box, task, config, limits, env):
            box.write("/run/lso/pi.jsonl", '{"type": "turn_start"}\n')
            box.write(config.events_path, '{"seq": 0}\n')
            raise RuntimeError("container went away")

    monkeypatch.setitem(ADAPTERS, "fake", lambda spec: Leaves())
    exp = experiment(repo, seeds=1, execution={"retries": 0})
    runner = make_runner(repo, tmp_path, exp=exp)
    assert [o.status for o in runner.run(task="toy-add")] == ["failed"]
    item = attempt_dir(tmp_path)
    assert (item / "pi.jsonl").read_text() == '{"type": "turn_start"}\n'
    assert (item / "events.jsonl").read_text() == '{"seq": 0}\n'


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
    advisor_json = (attempt_dir(tmp_path) / "advisor.json").read_text()
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


def split_manifest(repo, tmp_path):
    """The toy manifest with toy-add in `dev` and toy-max in `test`."""
    manifest = Manifest.from_yaml(repo / "tasks/manifests/toy-v1.yaml")
    tasks = [t.model_copy(update={"split": s}) for t, s in zip(manifest.tasks, ["dev", "test"])]
    path = tmp_path / "split.yaml"
    manifest.model_copy(update={"tasks": tasks}).to_yaml(path)
    return path


def test_test_split_needs_final(repo, tmp_path, agent):
    exp = experiment(repo, tasks=split_manifest(repo, tmp_path))
    runs = tmp_path / "runs"
    with pytest.raises(ConfigError, match="held-out `test` split.*sets no split"):
        make_runner(repo, runs, exp=exp).run()
    assert make_runner(repo, runs, exp=exp).ledger.rows("t") == []
    # Choosing only dev tasks needs no --final, even without a split.
    assert [o.status for o in make_runner(repo, runs, exp=exp).run(task="toy-add")] == ["done"] * 2
    test = exp.model_copy(update={"split": "test"})
    with pytest.raises(ConfigError, match="split is 'test'"):
        make_runner(repo, runs, exp=test).run()
    runner = make_runner(repo, runs, exp=test, final=True)
    assert [o.status for o in runner.run()] == ["done"] * 2
    sessions = runner.ledger.sessions("t")
    assert [(s["split"], s["test_tasks"], s["final"]) for s in sessions] == [
        (None, 0, 0),
        ("test", 1, 1),
    ]


class AdvisorAgent(FakeAgent):
    capabilities = frozenset({"advisor"})


class RecordingTracker:
    def __init__(self):
        self.summaries = {}
        self.arm_params = {}

    def log_item(self, key, arm_params, params, metrics, artifacts, trace=None):
        self.arm_params[key.arm] = arm_params
        return "run"

    def log_arm_summary(self, arm, config_hash, arm_params, metrics):
        self.summaries[arm] = metrics

    def close(self):
        pass


def test_advisor_arm_gets_prompts_and_paired_metrics(repo, tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "fake", lambda spec: AdvisorAgent())
    models = {
        "m": {"base_url": "http://x/v1", "model": "m"},
        "advisor": {"base_url": "http://y/v1", "model": "k"},
    }
    arms = [
        {"name": "A0", "agent": "fake", "executor": "m"},
        {"name": "H", "agent": "fake", "executor": "m", "advisor": {"level": "L2"}},
    ]
    exp = experiment(repo, models=models, arms=arms)
    tracker = RecordingTracker()
    make_runner(repo, tmp_path, exp=exp, tracker=tracker).run()
    config = json.loads((attempt_dir(tmp_path, "t/H/toy-add/seed-0") / "advisor.json").read_text())
    default = exp.prompt_set("default")
    assert config["prompts"]["hash"] == default.hash
    assert config["prompts"]["texts"]["advice_injection"] == default.texts.advice_injection
    assert tracker.arm_params["H"]["prompt_hash"] == default.hash
    assert tracker.arm_params["A0"]["prompt_hash"] is None
    assert tracker.summaries["H"]["paired_items"] == 4
    assert tracker.summaries["H"]["paired_diff"] == 0
    assert "paired_items" not in tracker.summaries["A0"]
