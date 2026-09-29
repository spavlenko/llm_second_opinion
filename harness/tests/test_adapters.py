"""Agent settings and the pi adapter without Docker; see test_pi_docker.py for real runs."""

import json
import shlex

import pytest

from llm_second_opinion.adapters import ADAPTERS
from llm_second_opinion.adapters.pi import PiAdapter, container_url, exit_reason, parse_jsonl
from llm_second_opinion.config import AgentSpec, ConfigError, Experiment
from llm_second_opinion.contracts import ExitReason, ModelEndpoint
from llm_second_opinion.runner import Runner


def experiment(repo, **overrides):
    raw = {
        "name": "t",
        "tasks": repo / "tasks/manifests/toy-v1.yaml",
        "seeds": 1,
        "limits": {"wall_minutes": 1, "max_turns": 5},
        "models": {"m": {"base_url": "http://x/v1", "model": "m"}},
        "arms": [{"name": "A0", "agent": "pi", "executor": "m"}],
    }
    return Experiment.model_validate({**raw, **overrides})


def test_short_form_means_adapter_defaults(repo):
    exp = experiment(repo)
    assert exp.agent_spec(exp.arms[0]) == AgentSpec(adapter="pi")


def test_agent_entry_is_in_the_config_hash_but_its_name_is_not(repo):
    arms = [{"name": "A0", "agent": "main", "executor": "m"}]
    base = experiment(repo, agents={"main": {"adapter": "pi", "version": "1"}}, arms=arms)
    renamed = experiment(
        repo,
        agents={"other": {"adapter": "pi", "version": "1"}},
        arms=[arms[0] | {"agent": "other"}],
    )
    bumped = experiment(repo, agents={"main": {"adapter": "pi", "version": "2"}}, arms=arms)
    h = base.config_hash(base.arms[0])
    assert renamed.config_hash(renamed.arms[0]) == h
    assert bumped.config_hash(bumped.arms[0]) != h


def test_option_typo_fails_before_anything_runs(repo, tmp_path):
    exp = experiment(
        repo,
        agents={"pi": {"adapter": "pi", "options": {"thinkng": "high"}}},
    )
    with pytest.raises(ConfigError, match="arm A0: agent 'pi' options"):
        Runner(exp, tmp_path, echo=lambda _: None)


def test_gold_takes_no_options():
    with pytest.raises(ConfigError):
        ADAPTERS["gold"](AgentSpec(adapter="gold", options={"x": 1}))


ENDPOINT = ModelEndpoint(
    base_url="http://127.0.0.1:8080/v1", model="qwen", reasoning_effort="low", api_key_env="KEY"
)


def test_command_uses_only_harness_extensions_and_the_executor():
    pi = PiAdapter(AgentSpec(adapter="pi", options={"tools": ["read", "bash"]}))
    argv = shlex.split(pi.command(ENDPOINT).split(" -- ")[0].removeprefix("exec "))
    assert argv[argv.index("--model") + 1] == "qwen"
    assert argv[argv.index("--thinking") + 1] == "low"  # from the endpoint
    assert argv[argv.index("--tools") + 1] == "read,bash"
    assert "--no-extensions" in argv and "--no-context-files" in argv and "--offline" in argv
    assert argv[argv.index("-e") + 1].endswith("/extensions/limits.ts")


def test_models_json_points_at_the_executor_through_the_host_gateway():
    pi = PiAdapter(AgentSpec(adapter="pi", options={"context_window": 32768}))
    provider = pi.models_json(ENDPOINT)["providers"]["lso"]
    assert provider["baseUrl"] == "http://host.docker.internal:8080/v1"
    assert provider["apiKey"] == "${KEY}"  # the name only; the key comes from the exec env
    assert provider["models"] == [
        {"id": "qwen", "name": "qwen", "reasoning": True, "contextWindow": 32768}
    ]


def test_container_url_rewrites_only_host_local_addresses():
    assert container_url("http://localhost/v1") == "http://host.docker.internal/v1"
    assert container_url("https://api.example.com/v1") == "https://api.example.com/v1"


def assistant_end(stop: str, error: str | None = None) -> dict:
    message = {"role": "assistant", "stopReason": stop, "errorMessage": error}
    return {"type": "message_end", "message": message}


@pytest.mark.parametrize(
    ("timed_out", "code", "limit", "events", "reason"),
    [
        (True, 124, {}, [], ExitReason.TIME_LIMIT),
        (False, 0, {"reason": "turn_limit", "turns": 3}, [], ExitReason.TURN_LIMIT),
        (False, 1, {}, [], ExitReason.CRASH),
        (False, 0, {}, [assistant_end("error", "404 exhausted")], ExitReason.CRASH),
        (False, 0, {}, [assistant_end("error"), assistant_end("stop")], ExitReason.FINISHED),
    ],
)
def test_exit_reason(timed_out, code, limit, events, reason):
    assert exit_reason(timed_out, code, limit, events, "stderr")[0] == reason


def test_parse_jsonl_splits_on_lf_only_and_skips_a_cut_record():
    text = json.dumps({"type": "a", "text": "x y"}) + "\n" + '{"type": "b"'
    assert parse_jsonl(text) == [{"type": "a", "text": "x y"}]
