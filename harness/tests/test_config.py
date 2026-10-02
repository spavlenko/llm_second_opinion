import json

import pytest
from pydantic import ValidationError

from llm_second_opinion.config import (
    ConfigError,
    Experiment,
    expand_sweeps,
    interpolate_env,
    load_dotenv,
)

ENV = {"LOCAL_MODEL_URL": "http://127.0.0.1:8080/v1", "ADVISOR_URL": "https://advisor/v1"}


def write(tmp_path, text):
    path = tmp_path / "exp.yaml"
    path.write_text(text)
    return path


BASE = """
name: t
tasks: manifest.yaml
seeds: 2
limits: {wall_minutes: 10, max_turns: 20}
models:
  local: {base_url: "http://x/v1", model: qwen}
  advisor: {base_url: "http://y/v1", model: kimi}
"""


def test_example_experiment_loads(repo):
    exp = Experiment.from_yaml(repo / "experiments/abstraction-sweep.yaml", env=ENV)
    assert [a.name for a in exp.arms] == ["A0", "A2", "A4"]
    assert exp.seeds == 3
    assert exp.models["local"].base_url == ENV["LOCAL_MODEL_URL"]
    assert exp.tasks == (repo / "tasks/manifests/cpp-arm64-v1.yaml").resolve()
    assert exp.arm("A2").advisor.level == "L2"
    assert exp.arm("A0").advisor is None


def test_missing_env_vars_listed(repo):
    with pytest.raises(ConfigError, match="ADVISOR_URL, LOCAL_MODEL_URL"):
        Experiment.from_yaml(repo / "experiments/abstraction-sweep.yaml", env={})


def test_env_default():
    assert interpolate_env({"a": ["${X:-d}", "${Y}"]}, {"Y": "y"}) == {"a": ["d", "y"]}


def test_sweep_expands_to_one_arm_per_value(tmp_path):
    exp = Experiment.from_yaml(
        write(
            tmp_path,
            BASE
            + """
arms:
  - name: A
    executor: local
    advisor: {level: [L1, L2, L3], max_consults: [1, 5]}
""",
        ),
        env={},
    )
    names = [a.name for a in exp.arms]
    assert names == ["A-L1-1", "A-L1-5", "A-L2-1", "A-L2-5", "A-L3-1", "A-L3-5"]
    assert exp.arm("A-L3-5").advisor.max_consults == 5


def test_interventions_list_is_not_a_sweep():
    arms = [{"name": "A", "advisor": {"level": "L2", "interventions": ["plan", "consult"]}}]
    assert expand_sweeps(arms) == arms


@pytest.mark.parametrize(
    "arms, message",
    [
        ("[{name: A, executor: nope}]", "not in models"),
        ("[{name: A, executor: local}, {name: A, executor: local}]", "duplicate arm names"),
        ("[{name: A, executor: local, advisor: {level: L9}}]", "level"),
        ("[{name: A, executor: local, colour: red}]", "colour"),
    ],
)
def test_invalid_arms(tmp_path, arms, message):
    with pytest.raises(ValidationError, match=message):
        Experiment.from_yaml(write(tmp_path, BASE + f"arms: {arms}\n"), env={})


def test_advisor_arm_needs_advisor_model(tmp_path):
    base = BASE.replace('  advisor: {base_url: "http://y/v1", model: kimi}\n', "")
    arms = "arms: [{name: A, executor: local, advisor: {level: L2}}]\n"
    with pytest.raises(ValidationError, match="no 'advisor'"):
        Experiment.from_yaml(write(tmp_path, base + arms), env={})


def test_config_hash_ignores_name_and_url_but_not_behaviour(tmp_path):
    exp = Experiment.from_yaml(
        write(tmp_path, BASE + "arms: [{name: A, executor: local, advisor: {level: L2}}]\n"),
        env={},
    )
    arm = exp.arm("A")
    h = exp.config_hash(arm)
    assert exp.config_hash(arm.model_copy(update={"name": "B"})) == h
    moved = exp.model_copy(deep=True)
    moved.models["local"].base_url = "http://elsewhere/v1"
    moved.models["local"].headers = {"X-Session": "me"}
    moved.models["local"].header_env = {"X-Auth": "AUTH"}
    assert moved.config_hash(arm) == h
    assert exp.config_hash(arm.with_advisor(level="L3")) != h
    other_model = exp.model_copy(deep=True)
    other_model.models["advisor"].model = "kimi-2"
    assert other_model.config_hash(arm) != h


def test_python_api_matches_spec(tmp_path):
    exp = Experiment.from_yaml(
        write(tmp_path, BASE + "arms: [{name: A2, executor: local, advisor: {level: L2}}]\n"),
        env={},
    )
    exp.arms.append(exp.arm("A2").with_advisor(level="L3", name="A3"))
    assert exp.arm("A3").advisor.level == "L3"
    assert exp.arm("A2").advisor.level == "L2"


def test_run_config_for_work_item(tmp_path):
    exp = Experiment.from_yaml(
        write(
            tmp_path,
            BASE + "arms: [{name: A0, executor: local}, {name: A2, executor: local,"
            " advisor: {level: L2}}]\n",
        ),
        env={},
    )
    plain = exp.run_config(exp.arm("A0"), task="t1", seed=1)
    assert plain.advisor is None and plain.advisor_model is None
    advised = json.loads(exp.run_config(exp.arm("A2"), task="t1", seed=1).model_dump_json())
    assert advised["run"] == {
        "experiment": "t",
        "arm": "A2",
        "task": "t1",
        "seed": 1,
        "config_hash": exp.config_hash(exp.arm("A2")),
    }
    assert advised["advisor_model"]["model"] == "kimi"
    assert advised["advisor"]["interventions"] == ["plan", "consult", "stuck"]


def test_load_dotenv_sets_unset_names_only(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# endpoints\n\nexport A=1\nB = 'two words'\nC=\"x=y\"\nKEEP=from-file\n")
    env = {"KEEP": "exported"}
    assert load_dotenv(path, env) == ["A", "B", "C"]
    assert env == {"KEEP": "exported", "A": "1", "B": "two words", "C": "x=y"}
    assert load_dotenv(tmp_path / "missing", env) == []


def test_load_dotenv_does_not_echo_a_bad_line(tmp_path):
    path = tmp_path / ".env"
    path.write_text("sk-secret-without-a-name\n")
    with pytest.raises(ConfigError) as e:
        load_dotenv(path, {})
    assert "sk-secret" not in str(e.value)


def test_split_and_task_ids_select_tasks_without_changing_the_hash(repo, tmp_path):
    from llm_second_opinion.tasks import Manifest

    manifest_path = repo / "tasks/manifests/mswe-mini-cpp-v1.yaml"
    manifest = Manifest.from_yaml(manifest_path)
    dev = [t.id for t in manifest.tasks if t.split == "dev"]
    text = BASE.replace("tasks: manifest.yaml", f"tasks: {manifest_path}")
    exp = Experiment.from_yaml(
        write(tmp_path, text + "split: dev\narms: [{name: A, executor: local}]\n"), env={}
    )
    assert [t.id for t in exp.select(manifest).tasks] == dev
    picked = exp.model_copy(update={"task_ids": dev[:2]})
    assert [t.id for t in picked.select(manifest).tasks] == dev[:2]
    assert picked.config_hash(exp.arms[0]) == exp.config_hash(exp.arms[0])
    test_task = next(t.id for t in manifest.tasks if t.split == "test")
    with pytest.raises(ConfigError, match=test_task):
        exp.model_copy(update={"task_ids": [test_task]}).select(manifest)


def test_mapping_keys_are_interpolated():
    raw = {"headers": {"${NAME}": "${VALUE}"}}
    assert interpolate_env(raw, {"NAME": "X-A", "VALUE": "v"}) == {"headers": {"X-A": "v"}}
