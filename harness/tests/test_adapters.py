"""Agent settings and the pi adapter without Docker; see test_pi_docker.py for real runs."""

import json
import shlex

import pytest

from llm_second_opinion.adapters import ADAPTERS
from llm_second_opinion.adapters.pi import (
    PiAdapter,
    container_url,
    endpoint_failure,
    exit_reason,
    parse_jsonl,
    pi_metrics,
)
from llm_second_opinion.config import AgentSpec, ConfigError, Experiment
from llm_second_opinion.contracts import ExitReason, ModelEndpoint
from llm_second_opinion.metrics import extract
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


def argv(command: str) -> list[str]:
    return shlex.split(command.split(" -- ")[0].removeprefix("exec "))


def test_advisor_arms_load_the_plugin():
    pi = PiAdapter(AgentSpec(adapter="pi"))
    assert "advisor" in pi.capabilities
    assert not any(a.endswith("lso-advisor.js") for a in argv(pi.command(ENDPOINT)))
    advised = argv(pi.command(ENDPOINT, advisor=True))
    extensions = [advised[i + 1] for i, a in enumerate(advised) if a == "-e"]
    assert extensions[-1] == "/opt/lso-agent/extensions/lso-advisor.js"


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


def test_models_json_references_secret_headers_by_name():
    endpoint = ENDPOINT.model_copy(
        update={"headers": {"X-Session": "me"}, "header_env": {"X-Auth": "AUTH_KEY"}}
    )
    options = {
        "thinking_level_map": {"off": "none", "high": "xhigh"},
        "compat": {"supportsDeveloperRole": False},
    }
    pi = PiAdapter(AgentSpec(adapter="pi", options=options))
    provider = pi.models_json(endpoint)["providers"]["lso"]
    assert provider["headers"] == {"X-Session": "me", "X-Auth": "${AUTH_KEY}"}
    [model] = provider["models"]
    assert model["thinkingLevelMap"] == {"off": "none", "high": "xhigh"}
    assert model["compat"] == {"supportsDeveloperRole": False}


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


@pytest.mark.parametrize(
    "events",
    [
        [assistant_end("error", "503 status code (no body)")],
        [assistant_end("error", '404: {"message":"recordings exhausted"}')],
        [assistant_end("error", "Connection error.")],
        [assistant_end("error", "fetch failed: ECONNREFUSED 192.168.1.5:8080")],
        [assistant_end("error", "Request timed out.")],
        [
            {"type": "auto_retry_start", "attempt": 1, "errorMessage": "529 overloaded"},
            {"type": "auto_retry_end", "success": False, "attempt": 3, "finalError": "529"},
            assistant_end("error", "529 overloaded"),
        ],
    ],
)
def test_an_endpoint_outage_is_infrastructure(events):
    assert exit_reason(False, 0, {}, events, "")[0] == ExitReason.CRASH
    assert endpoint_failure(events).startswith("model endpoint failed")


@pytest.mark.parametrize(
    "events",
    [
        [],  # pi exited non-zero on its own
        [assistant_end("error", "400: context length exceeded")],
        [assistant_end("error", "Unknown: tool schema invalid")],
        [assistant_end("error", '403: {"type":"token_limit"}')],  # the runner's token_limit
        [assistant_end("error", "503 down"), assistant_end("stop")],  # recovered
    ],
)
def test_a_genuine_crash_is_not_infrastructure(events):
    assert endpoint_failure(events) is None


def test_sampling_parameters_reach_models_json():
    endpoint = ENDPOINT.model_copy(update={"temperature": 0.0, "top_p": 0.95, "sampling_seed": 7})
    [model] = PiAdapter(AgentSpec(adapter="pi")).models_json(endpoint)["providers"]["lso"]["models"]
    assert model["samplingParams"] == {"temperature": 0.0, "top_p": 0.95, "seed": 7}
    [plain] = PiAdapter(AgentSpec(adapter="pi")).models_json(ENDPOINT)["providers"]["lso"]["models"]
    assert "samplingParams" not in plain


def test_fingerprint_names_the_prompt_and_the_bundle(monkeypatch):
    from llm_second_opinion.adapters import pi

    builds = []
    monkeypatch.setattr(pi, "_docker", lambda *a, **k: builds.append(a) or "sha256:abc\n")
    adapter = PiAdapter(AgentSpec(adapter="pi"))
    fingerprint = adapter.fingerprint()
    assert fingerprint["bundle_image"] == "sha256:abc" and len(fingerprint["prompt"]) == 16
    assert adapter.provenance()["pi_version"] == "0.99.1"
    assert len(builds) == 1  # built once per process


def test_metrics_from_a_recorded_pi_run(repo):
    item = repo / "harness/tests/fixtures/pi-item-consult"
    m = extract(item, PiAdapter(AgentSpec(adapter="pi")).metrics(item))
    assert (m["turns"], m["tool_calls"]) == (4, 3)
    assert m["tool_calls_by_name"] == {"bash": 2, "consult": 1}
    assert (m["test_runs"], m["test_runs_failed"]) == (1, 1)
    assert (m["first_test_turn"], m["first_edit_turn"], m["first_consult_turn"]) == (0, 2, 1)
    assert 0 <= m["first_test_s"] <= m["first_consult_s"] <= m["first_edit_s"]
    assert (m["compactions"], m["auto_retries"]) == (0, 0)
    assert (m["max_context_tokens"], m["final_context_tokens"]) == (1000, 1000)
    assert m["consults"] == 1 and m["consults_by_trigger"] == {"consult": 1}
    assert m["patch_touches_advised_files"] is True
    assert m["advised_files_touched"] == ["math.sh"]


def test_pi_metrics_count_compactions_retries_and_shell_edits():
    def bash(i, command):
        return {"type": "tool_execution_start", "toolCallId": i, "toolName": "bash",
                "args": {"command": command}}  # fmt: skip

    events = [
        {"type": "turn_start"},
        bash("a", "grep -n x src/a.cpp"),
        {"type": "auto_retry_start"},
        {"type": "auto_retry_end", "success": True},
        {"type": "turn_end"},
        {"type": "turn_start"},
        {"type": "compaction_start", "reason": "threshold"},
        bash("b", "/opt/lso/run-tests 2>&1 | tail -3"),
        {"type": "tool_execution_end", "toolCallId": "b", "isError": False,
         "result": {"content": [{"type": "text", "text": "50% tests passed, 2 tests failed out of 4"}]}},
        bash("c", "sed -i 's/a/b/' src/a.cpp"),
        {"type": "turn_end"},
    ]  # fmt: skip
    m = pi_metrics(events, [])
    assert (m["compactions"], m["auto_retries"], m["test_runs_failed"]) == (1, 1, 1)
    assert (m["first_edit_turn"], m["first_edit_s"]) == (1, None)


def test_parse_jsonl_splits_on_lf_only_and_skips_a_cut_record():
    text = json.dumps({"type": "a", "text": "x y"}) + "\n" + '{"type": "b"'
    assert parse_jsonl(text) == [{"type": "a", "text": "x y"}]
