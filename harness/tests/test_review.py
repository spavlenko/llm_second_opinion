import json
import shutil
import threading

import pytest

from llm_second_opinion.config import Experiment
from llm_second_opinion.mock_server import MockServer
from llm_second_opinion.picker import BASE_DIR, LOCAL_CHECK, compose, groups
from llm_second_opinion.review import CLI, Reviewer, shown
from llm_second_opinion.tasks import Task

BASE = {"passed": ["a", "b"], "failed": []}
P1 = "diff --git a/x.cpp b/x.cpp\n--- a/x.cpp\n+++ b/x.cpp\n@@ -1 +1 @@\n-int f();\n+int g();\n"
P2 = P1.replace("g()", "h()")
EXPERIMENT = """
name: rv
tasks: {manifest}
seeds: 6
limits: {{wall_minutes: 5, max_turns: 10}}
models:
  local: {{base_url: "http://unused.invalid/v1", model: local}}
  advisor: {{base_url: "{advisor}", model: adv}}
arms:
  - {{name: A0, agent: pi, executor: local}}
"""


def check(applied=True, build_failed=False, passed=("a", "b")):
    return {"applied": applied, "build_failed": build_failed, "timed_out": False,
            "passed": list(passed), "failed": []}  # fmt: skip


@pytest.fixture
def attempts(tmp_path):
    """Seeds 0-2: P1, P1 with other whitespace, P2 (only seed 2 resolves; the local rule
    picks seed 0, least reasoning). Seeds 3-5: empty, unappliable, P1: one usable."""
    (tmp_path / BASE_DIR).mkdir()
    (tmp_path / BASE_DIR / "t__r-1.json").write_text(json.dumps(BASE))
    spec = [
        (P1, check()),
        (P1.replace("int g", "int  g"), check()),
        (P2, check()),
        ("", check()),
        (P2, check(applied=False, passed=())),
        (P1, check(build_failed=True, passed=())),
    ]
    rows = []
    for seed, (patch, local) in enumerate(spec):
        d = tmp_path / f"s{seed}"
        d.mkdir()
        (d / "patch.diff").write_text(patch)
        (d / LOCAL_CHECK).write_text(json.dumps(local))
        rows.append({"task": "t__r-1", "seed": seed, "attempt_dir": f"s{seed}",
                     "resolved": int(seed == 2), "executor_reasoning_tokens": 10 * (seed + 1)})  # fmt: skip
    return rows


def test_review_shows_distinct_usable_candidates_in_a_fixed_shuffle(tmp_path, attempts):
    first, second = groups(tmp_path, attempts, 3)
    cands = shown(first)
    assert sorted(v.seed for v in cands) == [0, 2]  # seed 1 duplicates seed 0
    assert [v.seed for v in shown(first)] == [v.seed for v in cands]
    assert [v.seed for v in shown(second)] == [5]  # gated: no consult


@pytest.mark.skipif(not CLI.exists() or not shutil.which("node"), reason="plugin not built")
def test_review_asks_once_per_gated_group_and_keeps_the_record(tmp_path, attempts):
    first, _ = groups(tmp_path, attempts, 3)
    label = "AB"[[v.seed for v in shown(first)].index(2)]
    answer = (
        f"The other one renames the wrong function.\nRANKING: {label} > {'AB'.replace(label, '')}"
    )
    recordings = tmp_path / "advisor.jsonl"
    recordings.write_text(
        json.dumps({"message": {"content": answer}, "finish_reason": "stop"}) + "\n"
    )
    server = MockServer(("127.0.0.1", 0), recordings)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    task = Task(id="t__r-1", image="t:1", problem_statement="f is misnamed", eval_command="run")
    (tmp_path / "m.yaml").write_text(
        f"version: v1\ntasks:\n  - {json.dumps(task.model_dump(exclude_none=True))}\n"
    )
    (tmp_path / "rv.yaml").write_text(
        EXPERIMENT.format(manifest=tmp_path / "m.yaml", advisor=server.url)
    )
    exp = Experiment.from_yaml(tmp_path / "rv.yaml", env={})
    try:
        reviewer = Reviewer(exp, tmp_path, "L3", {}).start(None)
        try:
            records = {
                (g.task, g.s): reviewer.record(g, task) for g in groups(tmp_path, attempts, 3)
            }
        finally:
            reviewer.stop()
    finally:
        server.shutdown()
        server.server_close()
    assert server.served == 1 and reviewer.calls == 1 and not reviewer.errors
    r0, r1 = records[("t__r-1", 0)], records[("t__r-1", 1)]
    assert r0["consulted"] and not r0["fallback"] and r0["pick_seed"] == 2
    assert "Candidate A" in r0["result"]["brief"] and "f is misnamed" in r0["result"]["brief"]
    assert not r1["consulted"] and r1["pick_seed"] == 5
    usage = [
        json.loads(line) for line in (tmp_path / "review/usage.jsonl").read_text().splitlines()
    ]
    assert [u["role"] for u in usage] == ["advisor"]

    rows = compose(tmp_path, attempts, 3, records)
    assert [(r["local"], r["review"], r["review consulted"]) for r in rows] == [
        (0, 1, 1),
        (0, 0, 0),
    ]
    again = Reviewer(exp, tmp_path, "L3", {})  # records on disk: no proxy needed
    assert again.record(first, task) == r0 and again.calls == 0
