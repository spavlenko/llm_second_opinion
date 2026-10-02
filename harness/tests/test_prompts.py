import pytest
from pydantic import ValidationError

from llm_second_opinion.config import Experiment
from llm_second_opinion.contracts import PromptSlot
from llm_second_opinion.prompts import (
    PLACEHOLDERS,
    PromptError,
    PromptOverride,
    read_directory,
    resolve,
)

BASE = """
name: t
tasks: manifest.yaml
seeds: 1
limits: {wall_minutes: 10, max_turns: 20}
models:
  local: {base_url: "http://x/v1", model: qwen}
  advisor: {base_url: "http://y/v1", model: kimi}
"""
TEXTS = {
    "executor_guidance": "Ask for help at most {{max_consults}} times.",
    "consult_tool": "Ask the advisor.",
    "brief": "Issue: {{task_summary}}\nQuestion: {{question}}",
    "advisor_system": "Answer at level {{level}}.",
    "advice_injection": "Advice: {{advice}}",
}


def prompt_dir(path, **changes):
    path.mkdir(parents=True)
    for slot, text in (TEXTS | changes).items():
        if text is not None:
            (path / f"{slot}.md").write_text(text + "\n")
    return path


def load(tmp_path, text):
    path = tmp_path / "exp.yaml"
    path.write_text(BASE + text)
    return Experiment.from_yaml(path, env={})


def test_repo_prompt_sets_are_valid(repo):
    texts = read_directory(repo / "prompts/default")
    assert set(texts) == set(PromptSlot)
    for slot in ("structured/brief.md", "hints-only/advisor_system.md"):
        assert (repo / "prompts" / slot).is_file()


def test_override_chain_replaces_slots_and_keeps_the_rest(tmp_path):
    base = prompt_dir(tmp_path / "base")
    (tmp_path / "brief.md").write_text("Goal: {{task_summary}}\nHypothesis: {{hypothesis}}")
    (tmp_path / "advisor.md").write_text("Hints only, level {{level}}.")
    sources = {
        "base": base,
        "structured": PromptOverride(base="base", brief=tmp_path / "brief.md"),
        "hints": PromptOverride(base="structured", advisor_system=tmp_path / "advisor.md"),
    }
    hints = resolve("hints", sources)
    assert hints.name == "hints"
    assert hints.texts.brief.startswith("Goal:")
    assert hints.texts.advisor_system == "Hints only, level {{level}}."
    assert hints.texts.consult_tool == TEXTS["consult_tool"]
    assert len({resolve(n, sources).hash for n in sources}) == 3


def test_cycles_and_unknown_bases_are_rejected(tmp_path):
    sources = {"a": PromptOverride(base="b"), "b": PromptOverride(base="a")}
    with pytest.raises(PromptError, match="cycle: a -> b -> a"):
        resolve("a", sources)
    with pytest.raises(PromptError, match="no prompt set named 'nope' \\(base of 'c'\\)"):
        resolve("c", {"c": PromptOverride(base="nope")})


def test_unknown_placeholder_fails_with_file_and_name(tmp_path):
    bad = prompt_dir(tmp_path / "bad", advisor_system="Use {{advice}} and {{ level }}.")
    with pytest.raises(PromptError) as e:
        read_directory(bad)
    message = str(e.value)
    assert str(bad / "advisor_system.md") in message
    assert "{{advice}}, {{ level }}" in message
    assert "allowed: {{level}}, {{max_answer_tokens}}" in message


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"brief": None}, "missing prompt slot file\\(s\\): brief.md"),
        ({"consult_tool": "  \n"}, "consult_tool is empty"),
        ({"advice_injection": "Some advice arrived."}, "must use {{advice}}"),
        ({"notes": "x"}, "not a prompt slot: notes.md"),
    ],
)
def test_bad_prompt_directories(tmp_path, changes, message):
    with pytest.raises(PromptError, match=message):
        read_directory(prompt_dir(tmp_path / "p", **changes))


def test_every_slot_has_a_placeholder_list():
    assert set(PLACEHOLDERS) == set(PromptSlot)


def test_hash_covers_text_not_name_path_or_trailing_newline(tmp_path):
    one = resolve("one", {"one": prompt_dir(tmp_path / "one")})
    moved = prompt_dir(tmp_path / "elsewhere/two")
    (moved / "brief.md").write_text(TEXTS["brief"] + "\n\n")
    two = resolve("two", {"two": moved})
    assert two.hash == one.hash and len(one.hash) == 16
    (moved / "brief.md").write_text(TEXTS["brief"] + " Be specific.")
    assert resolve("two", {"two": moved}).hash != one.hash


def test_prompt_sets_in_yaml_are_relative_to_the_file(tmp_path):
    prompt_dir(tmp_path / "prompts/mine")
    (tmp_path / "prompts/brief.md").write_text("Q: {{question}}")
    exp = load(
        tmp_path,
        """
prompts:
  mine: prompts/mine
  short: {base: mine, brief: prompts/brief.md}
arms: [{name: H, executor: local, advisor: {level: L2, prompts: short}}]
""",
    )
    config = exp.run_config(exp.arm("H"), task="t1", seed=0)
    assert config.prompts.name == "short"
    assert config.prompts.texts.brief == "Q: {{question}}"
    assert config.prompts.hash == exp.prompt_set("short").hash


def test_default_set_is_the_repo_directory_unless_defined(repo, tmp_path):
    exp = load(tmp_path, "arms: [{name: H, executor: local, advisor: {level: L2}}]\n")
    assert (
        exp.prompt_set("default").texts
        == resolve("default", {"default": repo / "prompts/default"}).texts
    )
    prompt_dir(tmp_path / "mine")
    own = load(
        tmp_path,
        "prompts: {default: mine}\narms: [{name: H, executor: local, advisor: {level: L2}}]\n",
    )
    assert own.prompt_set("default").texts.brief == TEXTS["brief"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "arms: [{name: H, executor: local, advisor: {level: L2, prompts: nope}}]\n",
            "arm H: no prompt set named 'nope'",
        ),
        (
            "prompts: {broken: missing-dir}\narms: [{name: A, executor: local}]\n",
            "prompt set directory not found",
        ),
        (
            "prompts: {a: {base: b}, b: {base: a}}\narms: [{name: A, executor: local}]\n",
            "cycle",
        ),
        (
            "prompts: {a: {base: default, colour: x.md}}\narms: [{name: A, executor: local}]\n",
            "colour",
        ),
    ],
)
def test_bad_prompt_config_fails_at_load(tmp_path, text, message):
    with pytest.raises(ValidationError, match=message):
        load(tmp_path, text)
