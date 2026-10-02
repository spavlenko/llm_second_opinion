import csv

import pytest
from click.testing import CliRunner

from llm_second_opinion.cli import main
from llm_second_opinion.config import ConfigError, Experiment
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.report import (
    bootstrap_ci,
    format_pairs,
    format_pareto,
    mcnemar_exact,
    paired_comparisons,
    pareto,
    provenance,
)

EXPERIMENT = """
name: paired
tasks: {manifest}
seeds: 2
limits: {{wall_minutes: 10, max_turns: 20}}
models:
  local: {{base_url: "http://x/v1", model: qwen}}
  advisor: {{base_url: "http://y/v1", model: kimi}}
arms:
  - {{name: A0, executor: local}}
  - {{name: H, executor: local, advisor: {{level: L2}}}}
  - {{name: A4, executor: advisor}}
"""
# Resolved per task over seeds 0 and 1. H wins on t1 and t2 and loses one item on t4.
OUTCOMES = {
    "A0": {"t1": (0, 0), "t2": (0, 0), "t3": (1, 1), "t4": (1, 0)},
    "H": {"t1": (1, 1), "t2": (1, 0), "t3": (1, 1), "t4": (0, 0)},
    "A4": {"t1": (1, 1), "t2": (1, 1), "t3": (1, 1), "t4": (1, 1)},
}


@pytest.fixture
def exp(repo, tmp_path):
    path = tmp_path / "paired.yaml"
    path.write_text(EXPERIMENT.format(manifest=repo / "tasks/manifests/toy-v1.yaml"))
    return Experiment.from_yaml(path, env={})


def rows_for(exp, outcomes=OUTCOMES, **extra):
    """Ledger rows for `outcomes`; `extra` adds columns per arm (e.g. cost)."""
    done = {"status": "done", "duration_s": 1.0, "turns": 1, "exit_reason": "finished"}
    rows = []
    for arm, tasks in outcomes.items():
        h = exp.config_hash(exp.arm(arm))
        for task, resolved in tasks.items():
            for seed, r in enumerate(resolved):
                key = {"arm": arm, "task": task, "seed": seed, "config_hash": h}
                rows.append(key | done | {"resolved": r} | extra.get(arm, {}))
    return rows


@pytest.mark.parametrize(
    ("wins", "losses", "p"), [(0, 0, 1.0), (5, 0, 0.0625), (1, 9, 0.021484375), (3, 3, 1.0)]
)
def test_mcnemar_exact(wins, losses, p):
    assert mcnemar_exact(wins, losses) == pytest.approx(p)


def test_bootstrap_is_reproducible_and_degenerate_when_tasks_agree():
    groups = [(2, 2, 1), (2, 1, 0), (2, 0, 0), (2, 2, 2)]

    def diff(t):
        return (t[1] - t[2]) / t[0]

    first = bootstrap_ci(groups, diff, resamples=2000)
    assert first == bootstrap_ci(groups, diff, resamples=2000)
    low, high = first
    assert low <= diff([8, 5, 3]) <= high
    assert bootstrap_ci([(2, 1, 0)] * 5, diff, resamples=500) == (0.5, 0.5)
    assert bootstrap_ci(groups, lambda t: None, resamples=100) is None


def test_paired_comparisons(exp):
    rows = rows_for(exp)
    # Stale rows and items the baseline did not complete are not paired.
    rows.append({**rows[0], "config_hash": "stale", "task": "t9"})
    rows.append(
        {**rows[-1], "arm": "H", "task": "t9", "config_hash": exp.config_hash(exp.arm("H"))}
    )
    h, a4 = paired_comparisons(exp, rows, resamples=2000)
    assert (h.arm, h.baseline, h.ceiling, a4.ceiling) == ("H", "A0", "A4", None)
    assert (h.pairs, h.tasks) == (8, 4)
    assert (h.rate, h.baseline_rate, h.diff) == (5 / 8, 3 / 8, 2 / 8)
    assert (h.wins, h.losses) == (3, 1)
    assert h.mcnemar_p == pytest.approx(0.625)
    assert h.rel_lift == pytest.approx(2 / 3)
    assert h.diff_ci_low <= h.diff <= h.diff_ci_high
    assert h.gap_closed == pytest.approx((5 - 3) / (8 - 3))
    assert h.gap_ci_low <= h.gap_closed <= h.gap_ci_high
    assert a4.diff == pytest.approx(5 / 8)
    text = format_pairs([h, a4])
    assert "paired vs A0" in text and "gap closed (A4)" in text and "+25pp" in text


def test_gap_closed_is_undefined_when_the_ceiling_is_not_above_the_baseline(exp):
    flat = {**OUTCOMES, "A4": OUTCOMES["A0"]}
    h, _ = paired_comparisons(exp, rows_for(exp, flat), resamples=200)
    assert h.gap_closed is None and h.gap_ci_low is None
    assert h.diff == 2 / 8


def test_baseline_and_ceiling_can_be_chosen(exp):
    rows = rows_for(exp)
    pairs = paired_comparisons(exp, rows, baseline="H", ceiling="A4", resamples=200)
    assert [p.arm for p in pairs] == ["A0", "A4"]
    assert pairs[0].diff == -2 / 8
    with pytest.raises(ConfigError, match="no arm named 'B'"):
        paired_comparisons(exp, rows, baseline="B")
    without_a0 = exp.model_copy(update={"arms": exp.arms[1:]})
    assert paired_comparisons(without_a0, rows) == []
    assert "none" in format_pairs([])


def test_pareto_needs_cost_data(exp):
    assert pareto(exp, rows_for(exp)) is None
    assert "no cost data" in format_pareto(None)


def test_pareto_with_advisor_tokens(exp):
    tokens = {"advisor_prompt_tokens": 900, "advisor_completion_tokens": 100}
    rows = rows_for(exp, H=tokens, A0={"advisor_prompt_tokens": None}, A4=tokens)
    unit, points = pareto(exp, rows)
    by_arm = {p.arm: p for p in points}
    assert unit == "advisor tokens"
    assert by_arm["A0"].cost == 0 and by_arm["A0"].on_front
    assert by_arm["H"].cost == 1000 and by_arm["H"].cost_per_resolved == 1600
    # A4's executor is the advisor model, and its executor tokens are not in the ledger.
    assert by_arm["A4"].cost is None and not by_arm["A4"].on_front
    assert by_arm["H"].on_front


def test_pareto_with_cost_usd(exp):
    rows = rows_for(exp, H={"cost_usd": 0.02}, A4={"cost_usd": 0.5})
    unit, points = pareto(exp, rows)
    assert unit == "USD"
    assert [p.arm for p in points if p.on_front] == ["A0", "H", "A4"]
    dominated = rows_for(exp, H={"cost_usd": 0.9}, A4={"cost_usd": 0.5})
    assert [p.arm for p in pareto(exp, dominated)[1] if p.on_front] == ["A0", "A4"]
    assert "USD" in format_pareto(pareto(exp, rows))


def test_provenance_counts_every_variant_and_final_batches(exp):
    rows = rows_for(exp)
    rows.append({**rows[0], "config_hash": "stale-variant"})
    sessions = [{"final": 0, "test_tasks": 0}, {"final": 1, "test_tasks": 4}]
    text = provenance(exp, rows, sessions)
    assert "arms in the config: 3" in text
    assert "variants tried (distinct config hashes in the ledger): 4" in text
    assert "1 batch(es) run with --final" in text
    assert "WARNING" in provenance(exp, rows, [{"final": 0, "test_tasks": 2}])


def test_report_command_prints_and_writes_pairs(exp, repo, tmp_path):
    ledger = Ledger(tmp_path / "runs/paired/ledger.sqlite")
    for r in rows_for(exp):
        key = ItemKey("paired", r["arm"], r["task"], r["seed"], r["config_hash"])
        ledger.start(key)
        ledger.finish(key, "done", resolved=r["resolved"], exit_reason="finished", turns=1,
                      duration_s=1.0)  # fmt: skip
    yaml_path = tmp_path / "paired.yaml"
    pairs_csv = tmp_path / "pairs.csv"
    args = ["report", str(yaml_path), "--runs-dir", str(tmp_path / "runs")]
    result = CliRunner().invoke(main, [*args, "--pairs-csv", str(pairs_csv)])
    assert result.exit_code == 0, result.output
    assert "variants tried" in result.output
    assert "paired vs A0" in result.output
    assert "no cost data" in result.output
    rows = list(csv.DictReader(pairs_csv.open()))
    assert [r["arm"] for r in rows] == ["H", "A4"]
    assert float(rows[0]["diff"]) == 0.25
    bad = CliRunner().invoke(main, [*args, "--baseline", "nope"])
    assert bad.exit_code == 1 and "no arm named 'nope'" in bad.output
