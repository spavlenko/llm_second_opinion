"""Integration tests against a real Docker API; skipped when no daemon is reachable."""

import subprocess

import docker
import pytest
from click.testing import CliRunner
from mlflow import MlflowClient

from llm_second_opinion.cli import main
from llm_second_opinion.grading import grade
from llm_second_opinion.runtime import Runtime
from llm_second_opinion.tasks import Manifest

TOY_IMAGE = "llm-second-opinion/toy:1"

try:
    docker.from_env().ping()
except docker.errors.DockerException:
    pytest.skip("no Docker daemon", allow_module_level=True)

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def toy(repo):
    subprocess.run(["docker", "build", "-q", "-t", TOY_IMAGE, str(repo / "tasks/toy")], check=True)
    return {t.id: t for t in Manifest.from_yaml(repo / "tasks/manifests/toy-v1.yaml").tasks}


@pytest.fixture
def box(toy):
    box = Runtime().start(TOY_IMAGE, cpus=1, memory_gb=0.25, name="test")
    yield box
    box.remove()


def test_exec_write_read_and_env(box):
    box.write("/run/deep/file.txt", "hello")
    assert box.read("/run/deep/file.txt") == "hello"
    assert box.read("/missing") is None
    assert box.exec("echo $KEY", env={"KEY": "v"}).output == "v\n"
    assert box.exec("pwd", workdir="/testbed").output == "/testbed\n"


def test_exec_timeout(box):
    assert box.exec("sleep 5", timeout_s=1).timed_out


def test_wrong_patch_fails_grading(box, toy):
    task = toy["toy-add"]
    wrong = task.gold_patch.replace("$1 + $2", "$1 * $2")
    assert grade(box, task, wrong, timeout_s=30).reason == "tests_failed"


def test_gold_patch_resolves(box, toy):
    assert grade(box, toy["toy-max"], toy["toy-max"].gold_patch, timeout_s=30).resolved


def test_toy_experiment_end_to_end(repo, tmp_path, toy, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a SQLite store puts artifacts in ./mlruns
    exp = str(repo / "experiments/toy.yaml")
    runs = str(tmp_path)
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    args = ["run", exp, "--runs-dir", runs, "--parallel", "2", "--mlflow", uri]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "resolved 4/4" in result.output
    report = CliRunner().invoke(main, ["report", exp, "--runs-dir", runs])
    assert "4/4" in report.output
    assert not docker.from_env().containers.list(all=True, filters={"label": "llm-second-opinion"})

    client = MlflowClient(uri)
    experiment = client.get_experiment_by_name("toy")
    [arm] = client.search_runs([experiment.experiment_id], "tags.`lso.level` = 'arm'")
    assert arm.data.metrics["resolve_rate"] == 1.0 and arm.data.metrics["items_done"] == 4
    traces = client.search_traces(locations=[experiment.experiment_id])
    assert len(traces) == 4
    spans = traces[0].data.spans
    [root] = [s for s in spans if s.parent_id is None]
    assert root.name.startswith("gold/toy-")
    assert {s.name for s in spans} >= {"gold 1", "grade"}


def test_run_refuses_without_a_tracking_server(repo, tmp_path):
    exp = str(repo / "experiments/toy.yaml")
    args = ["run", exp, "--runs-dir", str(tmp_path), "--mlflow", "http://127.0.0.1:9"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0 and "MLflow is not reachable" in result.output
