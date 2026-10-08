import json
import threading

import pytest
from click.testing import CliRunner

from llm_second_opinion.cli import main
from llm_second_opinion.config import Experiment
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.tasks import Manifest, Task
from llm_second_opinion.uptake import consults, judge_text, parse_verdicts

GOLD = "diff --git a/p.hpp b/p.hpp\n--- a/p.hpp\n+++ b/p.hpp\n@@ -1 +1 @@\n-once\n+loop\n"
EXPERIMENT = """
name: up
tasks: {manifest}
seeds: 1
limits: {{wall_minutes: 5, max_turns: 10}}
models:
  local: {{base_url: "http://unused.invalid/v1", model: local}}
  advisor: {{base_url: "{judge}", model: adv}}
arms:
  - {{name: A0, agent: pi, executor: local}}
  - {{name: H, agent: pi, executor: local, advisor: {{level: L3}}}}
"""
VERDICT = (
    "**C1: right=yes followed=no | names the sign loop; the patch special-cases one sign**\n"
    "C2: right=Maybe followed=na | closing review\n"
)


def events(*pairs):
    out = []
    for i, (brief, advice) in enumerate(pairs, 1):
        out += [
            {"type": "advisor_request", "request_id": f"r{i}", "brief_text": brief},
            {"type": "advisor_response", "request_id": f"r{i}", "advice_text": "FILE a.cpp:1-5"},
            {"type": "advisor_response", "request_id": f"r{i}", "advice_text": advice},
        ]
    return out + [{"type": "advisor_request", "request_id": "r9", "brief_text": "unanswered"}]


def test_verdict_lines_and_consults_are_read():
    v = parse_verdicts(VERDICT + "noise\n")
    assert v[1] == {"right": "yes", "followed": "no",
                    "reason": "names the sign loop; the patch special-cases one sign"}  # fmt: skip
    assert v[2]["right"] == "na"  # an unknown grade
    items = consults(events(("Task\n\nTheir report:\nsaw X", "loop over signs"), ("plain", "Done")))
    assert [(c["report"], c["advice"]) for c in items] == [
        ("Their report:\nsaw X", "loop over signs"),  # the issue part is dropped; the last answer
        ("plain", "Done"),
    ]
    text = judge_text("signs", GOLD, items, "", False)
    assert "--- C2: advice\nDone" in text and "(not resolved" in text and "(empty)" in text


@pytest.fixture
def judge(tmp_path):
    server_file = tmp_path / "judge.jsonl"
    server_file.write_text(
        json.dumps({"message": {"content": VERDICT}, "finish_reason": "stop"}) + "\n"
    )
    server = MockServer(("127.0.0.1", 0), server_file)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_bench_uptake_judges_each_consult_once(tmp_path, judge):
    task = Task(id="acme__w-1", image="w:1", problem_statement="read every sign",
                gold_patch=GOLD, eval_command="run")  # fmt: skip
    Manifest(version="up-v1", tasks=[task]).to_yaml(tmp_path / "m.yaml")
    path = tmp_path / "up.yaml"
    path.write_text(EXPERIMENT.format(manifest=tmp_path / "m.yaml", judge=judge.url))
    exp = Experiment.from_yaml(path, env={})
    runs = tmp_path / "runs"
    ledger = Ledger(runs / "up/ledger.sqlite")
    for arm, evs in (("A0", []), ("H", events(("Their report: one sign", "loop"), ("r", "Done")))):
        key = ItemKey("up", arm, task.id, 0, exp.config_hash(exp.arm(arm)), "w:1")
        rel = key.dir() / "attempt-1"
        d = runs / "up" / rel
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
        (d / "patch.diff").write_text(GOLD)
        (d / "grade.json").write_text(json.dumps({"resolved": True}))
        ledger.start(key)
        ledger.finish(key, "done", resolved=True, attempt_dir=str(rel), turns=4, duration_s=1.0,
                      exit_reason="finished")  # fmt: skip
    args = ["uptake", str(path), "--runs-dir", str(runs), "--no-preflight"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "H: 2 consults; right 1, partly 0, wrong 0; right and followed 0/1" in result.output
    rows = [json.loads(line) for line in (runs / "up/uptake.jsonl").read_text().splitlines()]
    assert [(r["arm"], r["consult"], r["right"], r["followed"]) for r in rows] == [
        ("H", 1, "yes", "no"),
        ("H", 2, "na", "na"),
    ]
    (runs / "up/uptake.jsonl").write_text(
        (runs / "up/uptake.jsonl").read_text()
        + json.dumps({"arm": "X", "task": "t", "seed": 0})
        + "\n"
    )
    again = CliRunner().invoke(main, args + ["--arm", "H"])
    assert again.exit_code == 0 and "judge: 0 new call(s)" in again.output
    assert judge.served == 1
    kept = [json.loads(line)["arm"] for line in (runs / "up/uptake.jsonl").read_text().splitlines()]
    assert kept == ["X", "H", "H"]  # another arm's rows are kept
