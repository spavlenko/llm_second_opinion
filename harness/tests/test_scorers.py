import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from llm_second_opinion.cli import main
from llm_second_opinion.config import Experiment, Price
from llm_second_opinion.contracts import UsageRecord
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.scorers import (
    Attempt,
    Params,
    Prober,
    Truth,
    advice_copy_share,
    advice_uptake,
    advisor_spend,
    arm_means,
    brief_synthesis,
    consult_counts,
    flow_counts,
    format_diagnostics,
    gold_similarity,
    gold_units,
    leaked_units,
    numeric_scores,
    own_work,
    parse_guesses,
    partial_score,
    placeholders,
    probe_scores,
    redact_issue,
    role_map_leaks,
    score_attempt,
    score_experiment,
    truth_of,
)
from llm_second_opinion.tasks import Manifest, Task

FIXTURE = Path(__file__).parent / "fixtures/pi-item-consult"

GOLD = """\
diff --git a/include/widget/parser.hpp b/include/widget/parser.hpp
index 1111111..2222222 100644
--- a/include/widget/parser.hpp
+++ b/include/widget/parser.hpp
@@ -10,7 +10,7 @@ bool WidgetParser::parseNumber(const char* input)
 {
-    if (input[pos] == '-') return readSign(input);
+    while (input[pos] == '-') { return readSign(input, kMaxDepth); }
     return true;
 }
"""
ISSUE = "WidgetParser::parseNumber rejects `--5`; it should read every sign."
UNITS = ["WidgetParser", "kMaxDepth", "parseNumber", "readSign", "include/widget/parser.hpp"]


def diff(path: str, added: list[str], removed: list[str] = ()) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"
    )


# --- rewards ----------------------------------------------------------------------------


def test_partial_is_f2p_share_minus_a_penalty_per_broken_p2p():
    def grade(f2p, p2p_broken, **kw):
        return {"f2p": {"passed": f2p, "total": 4}, "p2p": {"passed": 10 - p2p_broken,
                "total": 10}, **kw}  # fmt: skip

    assert partial_score(grade(4, 0), resolved=True) == 1.0
    assert partial_score(grade(2, 0), resolved=False) == 0.5
    assert partial_score(grade(2, 1), resolved=False) == pytest.approx(0.4)
    assert partial_score(grade(1, 5), resolved=False) == 0.0  # clipped at 0
    assert partial_score(grade(3, 0, build_failed=True), resolved=False) == 0.0
    assert partial_score({"reason": "empty_patch"}, resolved=False) == 0.0
    assert partial_score({"reason": "tests_failed"}, resolved=False) == 0.0  # toy: no lists


def test_gold_similarity():
    assert gold_similarity(GOLD, GOLD) == 1.0
    assert gold_similarity("", GOLD) == 0.0
    assert gold_similarity(GOLD, "") is None
    half = diff("include/widget/parser.hpp", ["    while (input[pos] == '-') { return 1; }"])
    assert 0 < gold_similarity(half, GOLD) < 1
    two = diff("b.cpp", ["int b = 2;"]) + diff("a.cpp", ["int a = 1;"])
    flipped = diff("a.cpp", ["int a = 1;"]) + diff("b.cpp", ["int b = 2;"])
    assert gold_similarity(two, flipped) == 1.0  # files in path order


def test_gold_similarity_of_large_patches_is_by_lines(monkeypatch):
    half = diff("include/widget/parser.hpp", ["    while (input[pos] == '-') { return 1; }"])
    by_chars = gold_similarity(half, GOLD)
    monkeypatch.setattr("llm_second_opinion.scorers.SIMILARITY_CHARS", 10)
    assert gold_similarity(half, GOLD) == 0.0 < by_chars  # no line in common
    assert gold_similarity(GOLD, GOLD) == 1.0


def test_cost_and_exposure_penalties(tmp_path):
    usage = [
        UsageRecord(seq=0, ts=0, role="executor", model="l", prompt_tokens=5000,
                    completion_tokens=100, latency_ms=1, status=200),
        UsageRecord(seq=1, ts=0, role="advisor", model="a", prompt_tokens=2000,
                    completion_tokens=500, latency_ms=1, status=200),
        UsageRecord(seq=2, ts=0, role="advisor", model="a", prompt_tokens=0,
                    completion_tokens=0, latency_ms=1, status=503),
    ]  # fmt: skip
    price = Price(input_per_mtok=1000, output_per_mtok=2000)
    spent = advisor_spend(usage, cloud_executor=False, price=price)
    assert spent == {"advisor_calls": 1, "advisor_prompt_tokens": 2000,
                     "advisor_completion_tokens": 500, "advisor_cost_usd": 3.0}  # fmt: skip
    assert advisor_spend(usage, cloud_executor=True, price=price)["advisor_prompt_tokens"] == 7000
    assert advisor_spend(usage, False, None)["advisor_cost_usd"] is None  # no price
    assert advisor_spend(usage[:1], False, None)["advisor_cost_usd"] == 0.0  # no advisor calls

    d = tmp_path / "a"
    d.mkdir()
    (d / "usage.jsonl").write_text("".join(u.model_dump_json() + "\n" for u in usage))
    (d / "grade.json").write_text(json.dumps({"resolved": True}))
    task = Task(id="t", image="i", problem_statement=ISSUE, gold_patch=GOLD, eval_command="x")
    att = Attempt.load(tmp_path, "a", None, task, {"seed": 0, "config_hash": "h"})
    scores = score_attempt(att, Params(cost_lambda=0.1, exposure_mu=0.01), price)
    assert scores["cost_penalised"] == pytest.approx(1 - 0.1 * 3.0)
    assert scores["exposure_penalised"] == pytest.approx(1 - 0.01 * 2.0)  # 2 ktok
    leaked = score_attempt(att, Params(exposure_mu=0.5, exposure_unit="leaked"), price)
    assert leaked["leaked_units"] == 0.0 and leaked["exposure_penalised"] == 1.0
    assert score_attempt(att, Params(), None)["cost_penalised"] is None


# --- dependence ---------------------------------------------------------------------------


def test_advice_copy_share():
    patch = diff(
        "a.cpp",
        [
            "    return readSign(input, kMaxDepth);",  # in the advice, other spacing
            "    if (depth > kMaxDepth) throw parse_error(1);",  # fuzzy: one character off
            "    count += 1;",  # the executor's own
            "    }",  # trivial
            "    } else {",  # trivial
        ],
    )
    advice = [
        (
            "Change it to:\n```cpp\nreturn  readSign(input,  kMaxDepth);\n"
            "if (depth > kMaxDepth) throw parse_error(2);\n```"
        ),
        "Then check `count -= 42;` too.",
    ]
    share, copied, considered = advice_copy_share(patch, advice)
    assert (copied, considered) == (2, 3)
    assert share == pytest.approx(2 / 3)
    assert advice_copy_share(patch, []) == (0.0, 0, 3)  # no consult: nothing copied
    assert advice_copy_share("", advice) == (None, 0, 0)  # empty patch
    as_diff = ["```diff\n+    count += 1;\n```"]
    assert advice_copy_share(patch, as_diff)[1] == 1


def pi_run(tmp_path: Path) -> tuple[list[dict], list[dict]]:
    """A read, a test run, then (at t=3 s) a consult, then an edit."""

    def call(i, name, args, t, text):
        return (
            [{"type": "turn_start"},
             {"type": "tool_execution_start", "toolCallId": i, "toolName": name, "args": args},
             {"type": "message_end", "message": {"role": "toolResult", "toolCallId": i,
              "toolName": name, "content": [{"type": "text", "text": text}], "timestamp": t}},
             {"type": "turn_end"}],
            [{"t": t, "event": "tool_start", "id": i}],
        )  # fmt: skip

    error = "parser.hpp:12: error: no matching function for call to 'readSign(const char*&)'"
    steps = [
        call("r1", "read", {"path": "parser.hpp"}, 1000, "bool parseNumber(const char* input)"),
        call("b1", "bash", {"command": "/opt/lso/run-tests | tail"}, 2000, error),
        call("c1", "consult", {"question": "why?"}, 3000, "Advice: add kMaxDepth"),
        call("e1", "edit", {"path": "parser.hpp"}, 4000, "edited"),
        call("b2", "bash", {"command": "grep -rn readSign ."}, 5000, "late output " * 10),
    ]
    return [e for s in steps for e in s[0]], [m for s in steps for m in s[1]]


def test_own_work_before_the_first_consult(tmp_path):
    pi_events, timeline = pi_run(tmp_path)
    events = [{"type": "consult_requested", "turn": 2, "ts": 3.0001, "reason": "why?"},
              {"type": "trigger_fired", "turn": 4, "ts": 5.5, "intervention": "stuck"}]  # fmt: skip
    work = own_work(events, pi_events, timeline)
    assert work == {"own_turns_before_consult": 2, "own_tool_calls_before_consult": 2,
                    "own_reads_before_consult": 1, "own_edits_before_consult": 0,
                    "own_test_runs_before_consult": 1}  # fmt: skip
    none = own_work([], pi_events, timeline)
    assert set(none.values()) == {None}  # no consults
    no_timeline = own_work(events, pi_events, [])
    assert no_timeline["own_turns_before_consult"] == 2
    assert no_timeline["own_tool_calls_before_consult"] is None
    counts = consult_counts(events + [{"type": "consult_refused", "turn": 3}], turns=10)
    assert counts == {"consults": 2, "consult_rate": 0.2, "consult_refusals": 1}
    assert consult_counts([], None)["consult_rate"] is None


def test_brief_synthesis_share(tmp_path):
    pi_events, _ = pi_run(tmp_path)
    error = "parser.hpp:12: error: no matching function for call to 'readSign(const char*&)'"
    template = "A developer asks for advice.\n\nIssue:\n{{task_summary}}\n\nOutput:\n{{error}}"
    own = "I think the sign loop stops after one minus; should it loop until a digit?"
    events = [
        {"type": "brief_built", "role_map": {"<function_1>": "readSign"}},
        # L2: the error copied verbatim, with its name redacted; the executor's own words.
        {"type": "advisor_request", "ts": 3.0,
         "brief_text": f"A developer asks for advice.\n\nOutput:\n"
                       f"{error.replace('readSign', '<function_1>')}\n\n{own}"},
        # Pasting output the executor only sees later is not copying.
        {"type": "advisor_request", "ts": 3.5, "brief_text": "late output " * 10},
    ]  # fmt: skip
    share, per_brief = brief_synthesis(events, pi_events, ISSUE, template)
    first = len(own) / (len(error) + 1 + len(own))
    assert per_brief[0] == pytest.approx(first, abs=0.02)  # template lines left out
    assert per_brief[1] == 1.0
    assert 0 < share < 1
    issue_only = [{"type": "advisor_request", "ts": 9.0, "brief_text": f"Issue:\n{ISSUE}"}]
    assert brief_synthesis(issue_only, pi_events, ISSUE, template)[0] == 0.0
    assert brief_synthesis([], pi_events, ISSUE, template) == (None, [])


# --- exposure -----------------------------------------------------------------------------


def test_gold_units_are_names_and_files():
    identifiers, files = gold_units(GOLD)
    assert identifiers == UNITS[:4]  # input, pos: plain short words; '-': a literal
    assert files == UNITS[4:]


def test_leakage_at_l3_and_l2():
    l3 = "Fix WidgetParser::parseNumber in include/widget/parser.hpp: readSign is called once."
    l2 = "Fix <type_1>::<function_1> in <file_1>: <function_2> is called once."
    share, leaked, units = leaked_units([l3], GOLD)
    assert (share, units) == (0.8, 5)
    assert leaked == sorted(["WidgetParser", "include/widget/parser.hpp", "parseNumber",
                             "readSign"])  # fmt: skip
    assert leaked_units([l2], GOLD) == (0.0, [], 5)
    assert leaked_units(["see parser.hpp"], GOLD)[1] == ["include/widget/parser.hpp"]
    assert leaked_units([l3], "") == (None, [], 0)
    assert placeholders([l2, "<function_1> again"]) == (5, 4)


def test_role_map_leaks_follow_the_sweep_rule():
    events = [
        {"type": "brief_built", "role_map": {"<variable_1>": "kMaxDepth", "<function_1>": "parse"}},
        {"type": "advisor_request", "brief_text": "<function_1> uses kMaxDepth; parse it"},
        {"type": "brief_built", "role_map": {"<function_2>": "readSign"}},
        {"type": "advisor_request", "brief_text": "<function_2>"},
    ]
    assert role_map_leaks(events) == ["kMaxDepth"]  # `parse`: a plain short word


def test_scores_of_the_recorded_pi_run(repo):
    task = Manifest.from_yaml(repo / "tasks/manifests/toy-v1.yaml").tasks[0]
    att = Attempt.load(FIXTURE.parent, FIXTURE.name, None, task, {"seed": 0,
                       "config_hash": "h", "resolved": 1})  # fmt: skip
    scores = score_attempt(att, Params())
    assert scores["resolve"] == scores["partial"] == scores["gold_similarity"] == 1.0
    assert scores["consults"] == 1 and scores["consult_rate"] == 0.25  # 4 turns in pi.jsonl
    assert scores["own_test_runs_before_consult"] == 1
    assert scores["own_turns_before_consult"] == 1
    assert scores["advisor_prompt_tokens"] == 300 and scores["placeholders_sent"] == 4
    assert scores["leaked_units"] == 0.0
    assert all(k.startswith("score_") for k in numeric_scores(scores))
    # The advice names math.sh; the next turn edits it with sed.
    [c] = scores["uptake"]
    assert c["uptake"] == "acted" and c["located"] and "edited:math.sh" in c["signals"]
    assert scores["uptake_rate"] == 1.0
    assert scores["answer_overshoot"] == pytest.approx(10 / 250 - 1)  # 10 words, default target


# --- advice uptake -------------------------------------------------------------------------


def turn(*steps):
    """One pi turn: assistant text and tool calls, as pi.jsonl records."""
    out = [{"type": "turn_start"}]
    for step in steps:
        if isinstance(step, str):
            out.append({"type": "message_end", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": step}]}})  # fmt: skip
        else:
            name, args = step
            out.append({"type": "tool_execution_start", "toolName": name, "args": args})
    return out + [{"type": "turn_end"}]


def advice_result(text):
    return {"type": "message_end", "message": {"role": "toolResult", "toolName": "consult",
            "content": [{"type": "text", "text": text}]}}  # fmt: skip


ADVICE = "Look at <file_1>: <function_1> stops after one sign. Rerun the tests after the fix."
INJECTED = "Advice (1 left):\n\nLook at src/parser.cpp: readSign stops after one sign."
UPTAKE_EVENTS = [
    {"type": "consult_requested", "turn": 1},
    {"type": "brief_built", "role_map": {"<file_1>": "src/parser.cpp", "<function_1>": "readSign"}},
    {"type": "advisor_request", "request_id": "r1", "input_tokens": 300},
    {"type": "advisor_response", "request_id": "r1", "advice_text": ADVICE},
    {"type": "advice_applied", "request_id": "r1", "turn": 1, "injected_text": INJECTED},
]


def run_with(*later_turns):
    consult_turn = turn(("consult", {"question": "?"}))
    consult_turn.insert(-1, advice_result(INJECTED))
    return turn(("read", {"path": "README.md"})) + consult_turn + [e for t in later_turns
                                                                  for e in t]  # fmt: skip


def test_uptake_classes_from_signals():
    acted = run_with(turn(("read", {"path": "src/parser.cpp"})),
                     turn(("edit", {"path": "src/parser.cpp", "newText": "while"})))  # fmt: skip
    [c], summary = advice_uptake(UPTAKE_EVENTS, acted, 20)
    assert c["uptake"] == "acted" and c["located"]
    assert c["signals"] == ["read:parser.cpp", "edited:parser.cpp"]
    assert summary["uptake_rate"] == 1.0 and summary["uptake_acted"] == 1

    looked = run_with(turn(("bash", {"command": "grep -rn readSign src"})),
                      turn(("bash", {"command": "/opt/lso/run-tests | tail"})))  # fmt: skip
    [c], _ = advice_uptake(UPTAKE_EVENTS, looked, 20)
    assert c["signals"] == ["searched:readSign", "ran_tests"]
    assert c["uptake"] == "partial"  # one family: commands

    talked = run_with(turn("The advisor thinks readSign is wrong; checking.",
                           ("read", {"path": "src/parser.cpp"})))  # fmt: skip
    [c], _ = advice_uptake(UPTAKE_EVENTS, talked, 20)
    assert c["signals"] == ["text:readSign", "text:refers", "read:parser.cpp"]
    assert c["uptake"] == "acted"  # two families: text and files

    late = run_with(*[turn("thinking") for _ in range(5)], turn(("edit", {"path": "parser.cpp"})))
    [c], summary = advice_uptake(UPTAKE_EVENTS, late, 20)
    assert c["uptake"] == "ignored" and c["signals"] == []  # the edit is in turn 6 after
    assert summary["uptake_rate"] == 0.0 and summary["uptake_ignored"] == 1


def test_uptake_without_the_advice_in_pi_jsonl_counts_from_the_next_turn():
    pi = turn("start") + turn(("edit", {"path": "x.cpp"})) + turn(("edit", {"path": "parser.cpp"}))
    [c], _ = advice_uptake(UPTAKE_EVENTS, pi, None)
    assert not c["located"] and c["signals"] == ["edited:parser.cpp"]
    assert c["overshoot"] is None  # no target


def test_uptake_maps_surrogates_back():
    events = [dict(e) for e in UPTAKE_EVENTS]
    events[1] = {"type": "brief_built", "role_map": {"<file_1>": "src/parser.cpp",
                                                     "decodeSign": "readSign"}}  # fmt: skip
    events[3] = {"type": "advisor_response", "request_id": "r1",
                 "advice_text": "decodeSign stops after one sign."}  # fmt: skip
    pi = run_with(turn(("bash", {"command": "grep -n readSign src/parser.cpp"})))
    [c], _ = advice_uptake(events, pi, 20)
    assert "searched:readSign" in c["signals"]


def test_answer_overshoot_and_flow_counts():
    _, summary = advice_uptake(UPTAKE_EVENTS, [], 10)
    assert summary["answer_words"] == 14 and summary["answer_overshoot"] == pytest.approx(0.4)
    events = UPTAKE_EVENTS + [
        {"type": "trigger_skipped", "intervention": "stuck", "reason": "cooldown", "turn": 3},
        {"type": "trigger_skipped", "intervention": "stuck", "reason": "cooldown", "turn": 4},
        {"type": "trigger_skipped", "intervention": "orient", "reason": "reserved_for_end",
         "turn": 5},
        {"type": "advisor_followup", "request_id": "r1", "tokens": 120, "sent_text": "x"},
    ]  # fmt: skip
    counts = flow_counts(events)
    assert counts == {"trigger_skipped": 3, "trigger_skipped_cooldown": 2,
                      "trigger_skipped_reserved_for_end": 1, "advisor_followups": 1,
                      "advisor_followup_tokens": 120, "exposure_tokens": 420}  # fmt: skip
    records = [{"arm": "H", "scores": counts | {"uptake_rate": 0.5}}]
    text = format_diagnostics(arm_means(records), ["H"])
    assert "uptake" in text and "skipped triggers, H: cooldown 2, reserved_for_end 1" in text


# --- probe ---------------------------------------------------------------------------------


def test_truth_guesses_and_probe_scores():
    task = Task(id="acme__widget-7", image="i", problem_statement=ISSUE, gold_patch=GOLD,
                eval_command="x")  # fmt: skip
    truth = truth_of(task)
    assert truth == Truth("acme/widget", ["include/widget/parser.hpp"],
                          ["parseNumber", "readSign"])  # fmt: skip
    answer = 'Sure:\n{"repository": ["github.com/acme/widget"], "files": ["src/a.cpp", ' \
             '"widget/parser.hpp"], "functions": "WidgetParser::parseNumber()"}'  # fmt: skip
    guesses = parse_guesses(answer)
    assert guesses["functions"] == ["WidgetParser::parseNumber()"]
    scores = probe_scores(guesses, truth, "probe")
    assert scores == {"probe_repo_top1": 1.0, "probe_repo_top3": 1.0, "probe_file_top1": 0.0,
                      "probe_file_top3": 1.0, "probe_function_top1": 1.0,
                      "probe_function_top3": 1.0}  # fmt: skip
    assert parse_guesses("no idea") == {"repository": [], "files": [], "functions": []}
    in_class = (
        "diff --git a/f.h b/f.h\n--- a/f.h\n+++ b/f.h\n@@ -1,4 +1,4 @@ class specs_setter {\n"
        "   void on_zero() {\n-    if (a) b = 1;\n+    b = 2;\n   }\n"
    )
    assert truth_of(task.model_copy(update={"gold_patch": in_class})).functions == ["on_zero"]
    toy = probe_scores(guesses, Truth(None, [], []), "p")
    assert set(toy.values()) == {None}
    generic = Truth("nlohmann/json", [], [])
    assert probe_scores({"repository": ["json"], "files": [], "functions": []}, generic,
                        "p")["p_repo_top1"] == 0.0  # fmt: skip


def test_the_memorisation_floor_redacts_the_issue_like_the_run():
    issue = 'WidgetParser::parseNumber fails:\n```cpp\nparseNumber("--5");\n```'
    role_map = {"<type_1>": "WidgetParser", "<function_1>": "parseNumber"}
    assert redact_issue(issue, "L3", role_map) == issue
    assert redact_issue(issue, "L2", role_map) == (
        '<type_1>::<function_1> fails:\n```cpp\n<function_1>("--5");\n```'
    )
    assert redact_issue(issue, "L1", role_map) == "<type_1>::<function_1> fails:\n[code omitted]"


# --- an experiment -----------------------------------------------------------------------

EXPERIMENT = """
name: sc
tasks: {manifest}
seeds: 2
limits: {{wall_minutes: 5, max_turns: 10}}
models:
  local: {{base_url: "http://unused.invalid/v1", model: local}}
  advisor: {{base_url: "http://unused.invalid/v1", model: adv}}
  probe: {{base_url: "{probe}", model: probe-model}}
prices:
  advisor: {{input_per_mtok: 1000, output_per_mtok: 1000}}
  probe: {{input_per_mtok: 1000, output_per_mtok: 1000}}
arms:
  - {{name: A0, agent: pi, executor: local}}
  - {{name: H, agent: pi, executor: local, advisor: {{level: L2}}}}
  - {{name: H3, agent: pi, executor: local, advisor: {{level: L3}}}}
"""

ANSWERS = [  # in scoring order: H seed 0 (briefs, floor), H3 seed 0 (briefs, floor)
    {"repository": ["other/thing", "acme/widget"], "files": [], "functions": []},
    {"repository": ["other/thing"], "files": [], "functions": []},
    {"repository": ["acme/widget"], "files": ["parser.hpp"], "functions": ["parseNumber"]},
    {"repository": ["acme/widget"], "files": [], "functions": []},
]


@pytest.fixture
def mock(tmp_path):
    recordings = tmp_path / "probe.jsonl"
    lines = [{"message": {"content": json.dumps(a)}, "finish_reason": "stop"} for a in ANSWERS]
    recordings.write_text("".join(json.dumps(line) + "\n" for line in lines))
    server = MockServer(("127.0.0.1", 0), recordings)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def write_attempt(d: Path, *, events=(), patch="", grade=None, turns=4, usage=()):
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (d / "patch.diff").write_text(patch)
    (d / "grade.json").write_text(json.dumps(grade or {}))
    (d / "result.json").write_text(json.dumps({"turns": turns}))
    (d / "usage.jsonl").write_text("".join(u.model_dump_json() + "\n" for u in usage))


def consult(level, brief, role_map, advice, ts=1.0):
    return [
        {"type": "consult_requested", "turn": 1, "ts": ts, "reason": "?"},
        {"type": "brief_built", "level": level, "role_map": role_map},
        {"type": "advisor_request", "request_id": "r", "ts": ts, "brief_text": brief},
        {"type": "advisor_response", "request_id": "r", "advice_text": advice},
        {"type": "advice_applied", "request_id": "r", "turn": 1, "injected_text": advice},
    ]


@pytest.fixture
def experiment(tmp_path, mock):
    manifest = Manifest(
        version="sc-v1",
        tasks=[Task(id="acme__widget-7", image="widget:1", problem_statement=ISSUE,
                    gold_patch=GOLD, eval_command="run",
                    tests={"parser": "ctest", "fail_to_pass": ["f1", "f2"],
                           "pass_to_pass": ["p1"]})],
    )  # fmt: skip
    manifest.to_yaml(tmp_path / "manifest.yaml")
    path = tmp_path / "sc.yaml"
    path.write_text(EXPERIMENT.format(manifest=tmp_path / "manifest.yaml", probe=mock.url))
    exp = Experiment.from_yaml(path, env={})
    runs = tmp_path / "runs"
    ledger = Ledger(runs / "sc/ledger.sqlite")
    good = {"resolved": True, "f2p": {"passed": 2, "total": 2}, "p2p": {"passed": 1, "total": 1}}
    half = {"resolved": False, "f2p": {"passed": 1, "total": 2}, "p2p": {"passed": 1, "total": 1}}
    broken = {"resolved": False, "build_failed": True, "f2p": {"passed": 0, "total": 2}}
    advisor = UsageRecord(seq=0, ts=0, role="advisor", model="adv", prompt_tokens=1000,
                          completion_tokens=200, latency_ms=1, status=200)  # fmt: skip
    l2 = "Fix <type_1>::<function_1>: it calls <function_2> once. Uses kMaxDepth."
    l3 = "Fix WidgetParser::parseNumber in include/widget/parser.hpp; readSign is called once."
    role_map = {"<type_1>": "WidgetParser", "<function_1>": "parseNumber",
                "<function_2>": "readSign", "<variable_1>": "kMaxDepth"}  # fmt: skip
    advice = "Use:\n```cpp\nwhile (input[pos] == '-') { return readSign(input, kMaxDepth); }\n```"
    items = {
        # A0 seed 0: the layout before attempt directories (seed-0/ itself).
        ("A0", 0): (None, {"patch": GOLD, "grade": good}),
        ("A0", 1): ("attempt-1", {"patch": diff("x.cpp", ["int own = 1;"]), "grade": half}),
        ("H", 0): ("attempt-2", {"events": consult("L2", l2, role_map, advice), "patch": GOLD,
                                     "grade": good, "usage": [advisor]}),
        ("H", 1): ("attempt-1", {"patch": "", "grade": broken}),
        ("H3", 0): ("attempt-1", {"events": consult("L3", l3, {}, "Loop over the signs."),
                                      "patch": GOLD, "grade": good, "usage": [advisor]}),
    }  # fmt: skip
    for (arm, seed), (attempt, files) in items.items():
        key = ItemKey("sc", arm, "acme__widget-7", seed, exp.config_hash(exp.arm(arm)), "widget:1")
        rel = key.dir() / attempt if attempt else Path(arm) / key.task / f"seed-{seed}"
        write_attempt(runs / "sc" / rel, **files)
        ledger.start(key)
        ledger.finish(
            key,
            "done",
            resolved=files["grade"]["resolved"],
            attempt_dir=str(rel) if attempt else None,
            mlflow_run_id=f"run-{arm}{seed}",
            turns=4,
            duration_s=60.0,
            exit_reason="finished",
        )
    return exp, path, runs


class FakeTracker:
    def __init__(self):
        self.items, self.arms = {}, {}

    def log_metrics(self, run_id, metrics):
        self.items[run_id] = metrics

    def log_arm_summary(self, arm, config_hash, params, metrics):
        self.arms[arm] = metrics


def test_score_experiment_writes_scores_and_logs_to_mlflow(experiment):
    exp, _, runs = experiment
    tracker = FakeTracker()
    records = score_experiment(exp, runs, tracker=tracker, echo=lambda _: None)
    by = {(r["arm"], r["seed"]): r for r in records}
    assert len(by) == 5
    old = by[("A0", 0)]
    assert old["attempt_dir"] == "A0/acme__widget-7/seed-0"  # the old layout
    assert (runs / "sc" / old["attempt_dir"] / "scores.json").exists()
    assert by[("A0", 1)]["scores"]["partial"] == 0.5
    assert by[("A0", 1)]["scores"]["advice_copy_share"] == 0.0  # no advice
    assert by[("A0", 0)]["scores"]["consults_on_a0_solved"] is None
    h0, h1, h3 = by[("H", 0)]["scores"], by[("H", 1)]["scores"], by[("H3", 0)]["scores"]
    assert h0["consults_on_a0_solved"] == 1 and h1["consults_on_a0_solved"] == 0
    assert h0["advice_copy_share"] == 1.0  # the gold line, from the advice
    assert h1["partial"] == 0.0 and h1["gold_similarity"] == 0.0  # build failed, empty patch
    assert h1["advice_copy_share"] is None and h1["consults"] == 0
    assert h0["leaked_units_list"] == ["kMaxDepth"] and h0["role_map_leaks_list"] == ["kMaxDepth"]
    assert h3["leaked_units"] == 0.8 and h3["role_map_leaks"] == 0
    assert h0["cost_penalised"] == pytest.approx(1 - 1.2)  # $1.20 at 1000/Mtok
    lines = (runs / "sc/scores.jsonl").read_text().splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["scorer_version"] == 2
    assert json.loads(lines[0])["roles"]["resolve"] == "acceptance"
    assert tracker.items["run-H0"]["score_advice_copy_share"] == 1.0
    assert tracker.arms["H"]["score_mean_leaked_units"] == pytest.approx(0.1)
    assert tracker.arms["H"]["score_mean_uptake_rate"] == 0.0  # no pi.jsonl: nothing done
    assert all(isinstance(v, (int, float)) for m in tracker.arms.values() for v in m.values())
    assert tracker.arms["A0"]["score_mean_partial"] == 0.75


def test_bench_score_with_the_probe_and_report(experiment, mock):
    _exp, path, runs = experiment
    args = ["score", str(path), "--runs-dir", str(runs), "--no-mlflow", "--probe", "probe"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "probe: 4 new call(s)" in result.output
    assert mock.served == 4  # the preflight used no recording
    scores = {(r["arm"], r["seed"]): r["scores"] for r in map(json.loads,
              (runs / "sc/scores.jsonl").read_text().splitlines())}  # fmt: skip
    assert scores[("H", 0)]["probe_repo_top1"] == 0.0
    assert scores[("H", 0)]["probe_repo_top3"] == 1.0
    assert scores[("H", 0)]["probe_floor_repo_top3"] == 0.0
    assert scores[("H3", 0)]["probe_repo_top1"] == 1.0
    assert scores[("H3", 0)]["probe_function_top1"] == 1.0
    assert scores[("H3", 0)]["probe_floor_repo_top1"] == 1.0
    assert "probe_repo_top1" not in scores[("A0", 0)]  # nothing was sent
    h0 = (
        runs
        / "sc"
        / json.loads((runs / "sc/scores.jsonl").read_text().splitlines()[2])["attempt_dir"]
    )
    probe_usage = [json.loads(line) for line in (h0 / "probe-usage.jsonl").read_text().splitlines()]
    assert [u["role"] for u in probe_usage] == ["probe", "probe"]
    ledger = Ledger(runs / "sc/ledger.sqlite")
    assert len(ledger.probe("sc")) == 2 and len(ledger.preflight("sc")) == 1

    again = CliRunner().invoke(main, args + ["--no-preflight"])
    assert again.exit_code == 0, again.output
    assert "probe: 0 new call(s)" in again.output and mock.served == 4  # cached
    assert len(ledger.probe("sc")) == 2

    report = CliRunner().invoke(main, ["report", str(path), "--runs-dir", str(runs)])
    assert report.exit_code == 0, report.output
    assert "Diagnostics (not acceptance)" in report.output
    assert "probe repo@1" in report.output
    assert "probe " in report.output.split("spend (metered calls):")[1]
    record = json.loads((runs / "sc/report.json").read_text())
    assert record["diagnostics_not_acceptance"]["H3"]["probe_repo_top1"] == 1.0
    assert record["spend"]["probe"]["calls"] == 4


def test_a_failing_probe_is_recorded_not_fatal(experiment, tmp_path):
    exp, _, runs = experiment
    prober = Prober(exp, "probe", tmp_path / "cache", {}).start(None)
    prober.endpoint = prober.endpoint.model_copy(update={"base_url": "http://127.0.0.1:9/v1"})
    try:
        records = score_experiment(exp, runs, arm="H3", prober=prober, echo=lambda _: None)
    finally:
        prober.stop()
    [h3] = records
    assert "probe_error" in h3["scores"] and h3["scores"]["leaked_units"] == 0.8


def test_bench_score_needs_a_ledger_and_a_known_probe(experiment, tmp_path):
    _, path, runs = experiment
    empty = CliRunner().invoke(main, ["score", str(path), "--runs-dir", str(tmp_path / "x"),
                                      "--no-mlflow"])  # fmt: skip
    assert empty.exit_code != 0 and "no ledger" in empty.output
    bad = CliRunner().invoke(main, ["score", str(path), "--runs-dir", str(runs), "--no-mlflow",
                                    "--probe", "nope"])  # fmt: skip
    assert bad.exit_code != 0 and "not in models" in bad.output
