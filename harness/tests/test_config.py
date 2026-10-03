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
    assert exp.tasks == (repo / "tasks/manifests/mswe-mini-cpp-v2.yaml").resolve()
    assert exp.split == "dev"
    assert exp.arm("A2").advisor.level == "L2"
    assert exp.arm("A0").advisor is None
    assert set(exp.prompts) == {"structured", "hints-only"}
    assert exp.prompt_set("structured").texts.brief != exp.prompt_set("default").texts.brief


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


def test_prompts_and_interventions_sweep(tmp_path):
    (tmp_path / "structured.md").write_text("Q: {{question}}")
    exp = Experiment.from_yaml(
        write(
            tmp_path,
            BASE
            + """
prompts: {other: {base: default, brief: structured.md}}
arms:
  - name: H
    executor: local
    advisor:
      prompts: [default, other]
      interventions: [[consult], [consult, stuck], []]
      level: L2
""",
        ),
        env={},
    )
    names = [a.name for a in exp.arms]
    assert names == [
        "H-default-consult", "H-default-consult+stuck", "H-default-none",
        "H-other-consult", "H-other-consult+stuck", "H-other-none",
    ]  # fmt: skip
    assert exp.arm("H-other-consult+stuck").advisor.interventions == ["consult", "stuck"]
    assert exp.arm("H-other-none").advisor.interventions == []
    assert len({exp.config_hash(a) for a in exp.arms}) == 6


def test_arms_without_an_advisor_keep_their_hash(repo):
    # Pinned on 2026-10-03 (manifest version and adapter fingerprint added, unset model
    # fields dropped; no real model had run). Arms without an advisor have no prompt set,
    # so prompt-set changes must never move these.
    env = {"LOCAL_MODEL_URL": "u", "LOCAL_SESSION_HEADER": "h", "LOCAL_SESSION": "s",
           "LOCAL_AUTH_HEADER": "a"}  # fmt: skip
    baselines = Experiment.from_yaml(repo / "experiments/baselines.yaml", env=env)
    assert baselines.config_hash(baselines.arm("A0")) == "8c03ef1b79a37500"  # + executor sampling
    assert (
        baselines.config_hash(baselines.arm("A4")) == "914bf24f2bc9a105"
    )  # pi-advisor compat, model k3
    toy = Experiment.from_yaml(repo / "experiments/toy.yaml")
    assert toy.config_hash(toy.arm("gold")) == "84562adef09adc5f"


def test_advisor_arm_hash_is_pinned(repo):
    # Pinned on 2026-10-03 with the pre-pilot default prompts (executor does the work, the
    # advisor gives a second opinion) and contract defaults (consult rules, size targets,
    # max_answer_tokens 4000). Moving either is deliberate: update these, and the Decision log.
    exp = Experiment.from_yaml(repo / "experiments/abstraction-sweep.yaml", env=ENV)
    assert exp.prompt_set("default").hash == "aaad3466794d6869"
    assert exp.config_hash(exp.arm("A2")) == "93c269647703cb1c"
    rules = exp.arm("A2").advisor.rules
    assert (rules.min_own_actions, rules.tool_cooldown_turns, rules.require_hypothesis) == (
        1, 2, True,
    )  # fmt: skip


def test_config_hash_follows_prompt_text_not_set_name(tmp_path):
    (tmp_path / "a.md").write_text("Q: {{question}}")
    (tmp_path / "b.md").write_text("Q: {{question}}\n")
    (tmp_path / "c.md").write_text("Question: {{question}}")
    exp = Experiment.from_yaml(
        write(
            tmp_path,
            BASE
            + """
prompts:
  a: {base: default, brief: a.md}
  b: {base: default, brief: b.md}
  c: {base: default, brief: c.md}
arms:
  - {name: A, executor: local, advisor: {level: L2, prompts: a}}
  - {name: B, executor: local, advisor: {level: L2, prompts: b}}
  - {name: C, executor: local, advisor: {level: L2, prompts: c}}
""",
        ),
        env={},
    )
    a, b, c = (exp.config_hash(arm) for arm in exp.arms)
    assert a == b  # same text under another name and file
    assert a != c


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
    assert advised["advisor"]["prompts"] == "default"
    assert advised["prompts"]["name"] == "default"
    assert advised["prompts"]["hash"] == exp.prompt_set("default").hash
    assert set(advised["prompts"]["texts"]) == {
        "executor_guidance", "consult_tool", "brief", "advisor_system", "advice_injection"
    }  # fmt: skip
    assert plain.prompts is None


def test_load_dotenv_sets_unset_names_only(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# endpoints\n\nexport A=1\nB = 'two words'\nC=\"x=y\"\nKEEP=from-file\nBLANK=\n"
    )
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

    manifest_path = repo / "tasks/manifests/mswe-mini-cpp-v2.yaml"
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
