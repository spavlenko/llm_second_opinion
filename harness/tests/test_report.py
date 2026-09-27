import pytest

from llm_second_opinion.config import Experiment
from llm_second_opinion.report import format_table, summarize, wilson_interval


@pytest.mark.parametrize(
    ("k", "n", "expected"),
    [(0, 10, (0.0, 0.2775)), (5, 10, (0.2366, 0.7634)), (10, 10, (0.7225, 1.0))],
)
def test_wilson_interval_matches_reference_values(k, n, expected):
    low, high = wilson_interval(k, n)
    assert low == pytest.approx(expected[0], abs=1e-4)
    assert high == pytest.approx(expected[1], abs=1e-4)


def _row(arm, h, status="done", resolved=1, seed=0):
    return {"arm": arm, "task": "t", "seed": seed, "config_hash": h, "status": status,
            "resolved": resolved, "duration_s": 60.0, "turns": 3, "exit_reason": "finished"}  # fmt: skip


def test_summarize_ignores_stale_hashes_and_counts_failures(repo):
    exp = Experiment.from_yaml(repo / "experiments/toy.yaml")
    h = exp.config_hash(exp.arm("gold"))
    rows = [
        _row("gold", h, seed=0),
        _row("gold", h, resolved=0, seed=1),
        _row("gold", h, status="failed", resolved=None, seed=2),
        _row("gold", "stale0000000000", seed=3),
        _row("removed-arm", h, seed=0),
    ]
    [s] = summarize(exp, rows, tasks=2)
    assert (s.planned, s.done, s.failed, s.resolved, s.rate) == (4, 2, 1, 1, 0.5)
    assert "gold" in format_table([s])
