"""Integration tests against a real Docker API; skipped when no daemon is reachable."""

import subprocess

import docker
import pytest
from click.testing import CliRunner
from mlflow import MlflowClient

from llm_second_opinion.cli import main
from llm_second_opinion.grading import grade, run_tests
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


# The toy repository plus a committed test file, as task images have.
WITH_TESTS = (
    "mkdir -p tests && printf '. ./math.sh\\n[ \"$(add 2 3)\" = 5 ] || exit 1\\n' "
    "> tests/test_add.sh && git add -A && git -c user.name=t -c user.email=t@t commit -qm tests"
)


def test_agent_edits_to_test_patch_files_are_reset_before_grading(toy):
    """The agent fixes the bug and also edits the test file the test patch modifies: as in
    SWE-bench, the file is reset to base, the test patch applies, and the item resolves."""
    runtime = Runtime()
    maker = runtime.start(TOY_IMAGE, cpus=1, memory_gb=0.25, name="test")
    try:
        maker.exec(WITH_TESTS, workdir="/testbed")
        maker.exec(
            "sed -i 's/$1 - $2/$1 + $2/' math.sh && echo 'echo agent was here' >> tests/test_add.sh",
            workdir="/testbed",
        )
        patch = maker.exec("git diff", workdir="/testbed").output
        maker.exec("git checkout -q -- . && echo '[ \"$(add 1 1)\" = 2 ] || exit 1' "
                   ">> tests/test_add.sh", workdir="/testbed")  # fmt: skip
        test_patch = maker.exec("git diff", workdir="/testbed").output
    finally:
        maker.remove()
    task = toy["toy-add"].model_copy(update={"test_patch": test_patch})
    for reset, expected in ((True, "resolved"), (False, "test_patch_failed")):
        box = runtime.start(TOY_IMAGE, cpus=1, memory_gb=0.25, name="test")
        try:
            box.exec(WITH_TESTS, workdir="/testbed")
            if reset:
                graded = grade(box, task, patch, timeout_s=30)
                assert graded.agent_touched_test_files == ["tests/test_add.sh"]
            else:  # without the reset, as before: the test patch does not apply
                graded = run_tests(box, task, [("patch_failed", patch)], 30)
            assert graded.reason == expected, graded.log
        finally:
            box.remove()


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
    # Only this run's containers: other batches may be running on the same daemon.
    left = docker.from_env().containers.list(all=True, filters={"label": "llm-second-opinion"})
    assert not [c for c in left if "toy-" in c.labels["llm-second-opinion"]]

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


def test_agent_run_tests_repeats_a_build_failure_at_the_end(box, repo):
    from llm_second_opinion.adapters.pi import INSTALL_RUN_TESTS, MOUNT

    box.write(f"{MOUNT}/bin/run-tests", (repo / "agents/pi/run-tests").read_text())
    box.exec(f"chmod +x {MOUNT}/bin/run-tests")
    assert box.exec(INSTALL_RUN_TESTS).exit_code == 0  # no run-tests in the image: left alone
    assert box.read("/opt/lso/run-tests") is None
    image_script = (
        "#!/bin/sh\n[ -n \"$FAIL\" ] && echo 'run-tests: build failed; last lines:'\n"
        "seq 1 50\necho '50% tests passed, 1 tests failed out of 2'\nexit 8\n"
    )
    box.write("/opt/lso/run-tests", image_script)
    box.exec("chmod +x /opt/lso/run-tests")
    for _ in range(2):  # a second install keeps the image's script
        assert box.exec(INSTALL_RUN_TESTS).exit_code == 0
    assert box.read("/opt/lso/run-tests.image") == image_script
    failed = box.exec("/opt/lso/run-tests | tail -n 2", env={"FAIL": "1"}).output
    assert "tests failed out of 2" in failed and "run-tests: build failed" in failed
    built = box.exec("/opt/lso/run-tests").output
    assert "build failed" not in built and built.rstrip().endswith("out of 2")
    assert box.exec("/opt/lso/run-tests >/dev/null").exit_code == 8


def test_local_check_never_applies_the_hidden_test_patch(box, toy):
    from llm_second_opinion.picker import local_check

    task = toy["toy-add"]
    gold = local_check(box, task, task.gold_patch, timeout_s=30)
    assert gold["applied"] and not gold["build_failed"] and not gold["timed_out"]
    assert box.exec("test -e tests/test_add.sh", workdir=task.workdir).exit_code != 0
    assert not local_check(box, task, "not a diff\n", timeout_s=30)["applied"]
