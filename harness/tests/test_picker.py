import json

from llm_second_opinion.picker import (
    BASE_DIR,
    LOCAL_CHECK,
    Visible,
    compose,
    pick_least_reasoning,
    pick_local,
)

BASE = {"passed": ["a", "b", "c"], "failed": []}


def check(passed=("a", "b", "c"), build_failed=False, applied=True, timed_out=False):
    return {
        "applied": applied,
        "build_failed": build_failed,
        "timed_out": timed_out,
        "passed": list(passed),
        "failed": [],
    }


def test_local_prefers_a_build_then_fewest_broken_then_least_reasoning():
    broke_build = Visible(0, False, check(passed=(), build_failed=True), 10)
    breaks_one = Visible(1, False, check(passed=("a", "b")), 20)
    clean_slow = Visible(2, False, check(), 900)
    clean_fast = Visible(3, False, check(), 500)
    cands = [broke_build, breaks_one, clean_slow, clean_fast]
    assert pick_local(cands, BASE) is clean_fast
    assert pick_local([broke_build, breaks_one], BASE) is breaks_one
    assert pick_least_reasoning(cands, BASE) is broke_build


def test_empty_or_unappliable_patches_are_picked_only_when_nothing_else_is_left():
    empty = Visible(0, True, check(), 1)
    unapplied = Visible(1, False, check(applied=False, passed=()), 2)
    real = Visible(2, False, check(passed=(), build_failed=True), 3)
    assert pick_local([empty, unapplied, real], BASE) is real
    assert pick_least_reasoning([empty, unapplied, real], BASE) is real
    assert pick_local([empty, unapplied], BASE) is empty


def test_compose_scores_each_rule_by_grade_without_showing_it_to_the_rules(tmp_path):
    (tmp_path / BASE_DIR).mkdir()
    (tmp_path / BASE_DIR / "t.json").write_text(json.dumps(BASE))
    rows = []
    # seeds 0-2: only seed 1 resolves, and it also has the clean local check
    # seeds 3-5: none resolves; seed 6 alone is an incomplete group and is left out
    for seed in range(7):
        d = tmp_path / f"s{seed}"
        d.mkdir()
        (d / "patch.diff").write_text("diff --git a/x b/x\n+y\n")
        clean = seed in (1, 4)
        (d / LOCAL_CHECK).write_text(json.dumps(check(build_failed=not clean)))
        rows.append(
            {
                "task": "t",
                "seed": seed,
                "attempt_dir": f"s{seed}",
                "resolved": int(seed == 1),
                "executor_reasoning_tokens": 100 - seed,
            }
        )
    got = compose(tmp_path, rows, 3)
    assert [(r["seed"], r["candidates"]) for r in got] == [(0, "0/1/2"), (1, "3/4/5")]
    first, second = got
    assert first["oracle"] == 1 and abs(first["mean"] - 1 / 3) < 1e-9
    assert first["local"] == 1 and first["local seed"] == 1
    assert first["least reasoning"] == 0 and first["least reasoning seed"] == 2
    assert second["oracle"] == 0 and second["local"] == 0


def test_compose_leaves_out_groups_with_a_missing_check(tmp_path):
    (tmp_path / BASE_DIR).mkdir()
    (tmp_path / BASE_DIR / "t.json").write_text(json.dumps(BASE))
    rows = []
    for seed in range(3):
        (tmp_path / f"s{seed}").mkdir()
        if seed:
            (tmp_path / f"s{seed}" / LOCAL_CHECK).write_text(json.dumps(check()))
        rows.append(
            {"task": "t", "seed": seed, "attempt_dir": f"s{seed}", "resolved": 0,
             "executor_reasoning_tokens": 1}
        )  # fmt: skip
    assert compose(tmp_path, rows, 3) == []
