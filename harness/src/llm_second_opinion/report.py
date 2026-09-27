"""Per-arm summaries from the ledger, for the terminal and for analysis as CSV."""

from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_second_opinion.config import Experiment

ITEM_COLUMNS = [
    "arm", "task", "seed", "config_hash", "status", "attempts", "resolved", "grade",
    "exit_reason", "turns", "duration_s", "error", "mlflow_run_id",
]  # fmt: skip


@dataclass
class ArmSummary:
    arm: str
    config_hash: str
    planned: int
    done: int = 0
    failed: int = 0
    resolved: int = 0
    durations: list[float] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    exit_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def rate(self) -> float | None:
        return self.resolved / self.done if self.done else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.resolved, self.done) if self.done else None


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (95% by default).

    Preferred over the normal approximation because it stays inside [0, 1] and behaves
    at the small n and extreme rates typical of per-arm resolve rates.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def current_rows(exp: Experiment, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger rows for the experiment's arms at their current config hash; stale rows are dropped."""
    hashes = {arm.name: exp.config_hash(arm) for arm in exp.arms}
    return [r for r in rows if hashes.get(r["arm"]) == r["config_hash"]]


def summarize(exp: Experiment, rows: list[dict[str, Any]], tasks: int) -> list[ArmSummary]:
    summaries = {
        arm.name: ArmSummary(arm.name, exp.config_hash(arm), planned=tasks * exp.seeds)
        for arm in exp.arms
    }
    for row in current_rows(exp, rows):
        s = summaries[row["arm"]]
        if row["status"] == "failed":
            s.failed += 1
        if row["status"] != "done":
            continue
        s.done += 1
        s.resolved += bool(row["resolved"])
        s.durations.append(row["duration_s"])
        s.turns.append(row["turns"])
        s.exit_reasons[row["exit_reason"]] += 1
    return list(summaries.values())


def format_table(summaries: list[ArmSummary]) -> str:
    header = f"{'arm':<12} {'done':>9} {'failed':>6} {'resolved':>8} {'rate':>6} {'95% CI':>13} "
    header += f"{'min/item':>8} {'turns':>6}  exit reasons"
    lines = [header, "-" * len(header)]
    for s in summaries:
        rate = f"{s.rate:.0%}" if s.rate is not None else "-"
        ci = f"{s.interval[0]:.0%}-{s.interval[1]:.0%}" if s.interval else "-"
        minutes = f"{_mean(s.durations) / 60:.1f}" if s.durations else "-"
        turns = f"{_mean(s.turns):.1f}" if s.turns else "-"
        reasons = ", ".join(f"{k} {v}" for k, v in s.exit_reasons.most_common())
        lines.append(
            f"{s.arm:<12} {f'{s.done}/{s.planned}':>9} {s.failed:>6} {s.resolved:>8} {rate:>6} "
            f"{ci:>13} {minutes:>8} {turns:>6}  {reasons}"
        )
    return "\n".join(lines)


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    """One row per work item, for analysis in pandas or R."""
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, ITEM_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
