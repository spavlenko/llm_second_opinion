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
    "executor_prompt_tokens", "executor_completion_tokens", "advisor_prompt_tokens",
    "advisor_completion_tokens", "model_calls", "cost_usd",
]  # fmt: skip
TOKEN_COLUMNS = ITEM_COLUMNS[-6:-2]


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
    # Metered done items only: tokens (all roles) and advisor tokens per item.
    tokens: list[int] = field(default_factory=list)
    advisor_tokens: list[int] = field(default_factory=list)
    # Cost per done item; None where an item was not metered or a model had no price.
    costs: list[float | None] = field(default_factory=list)

    @property
    def cost(self) -> float | None:
        """Total cost of the done items, or None unless every one has a cost: a sum that
        silently skipped items would understate it."""
        if not self.costs or any(c is None for c in self.costs):
            return None
        return sum(self.costs)

    @property
    def cost_per_resolved(self) -> float | None:
        cost = self.cost
        return cost / self.resolved if cost is not None and self.resolved else None

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
        s.costs.append(row.get("cost_usd"))
        if row.get("model_calls") is not None:
            s.tokens.append(sum(row[c] or 0 for c in TOKEN_COLUMNS))
            s.advisor_tokens.append(sum(row[c] or 0 for c in TOKEN_COLUMNS[2:]))
    return list(summaries.values())


def format_table(summaries: list[ArmSummary]) -> str:
    header = f"{'arm':<12} {'done':>9} {'failed':>6} {'resolved':>8} {'rate':>6} {'95% CI':>13} "
    header += f"{'min/item':>8} {'turns':>6} {'ktok/item':>9} {'adv ktok':>8} {'cost $':>8} "
    header += f"{'$/resolved':>10}  exit reasons"
    lines = [header, "-" * len(header)]
    for s in summaries:
        rate = f"{s.rate:.0%}" if s.rate is not None else "-"
        ci = f"{s.interval[0]:.0%}-{s.interval[1]:.0%}" if s.interval else "-"
        minutes = f"{_mean(s.durations) / 60:.1f}" if s.durations else "-"
        turns = f"{_mean(s.turns):.1f}" if s.turns else "-"
        tokens = f"{_mean(s.tokens) / 1000:.1f}" if s.tokens else "-"
        advisor = f"{_mean(s.advisor_tokens) / 1000:.1f}" if s.advisor_tokens else "-"
        cost = f"{s.cost:.2f}" if s.cost is not None else "-"
        per_resolved = f"{s.cost_per_resolved:.3f}" if s.cost_per_resolved is not None else "-"
        reasons = ", ".join(f"{k} {v}" for k, v in s.exit_reasons.most_common())
        lines.append(
            f"{s.arm:<12} {f'{s.done}/{s.planned}':>9} {s.failed:>6} {s.resolved:>8} {rate:>6} "
            f"{ci:>13} {minutes:>8} {turns:>6} {tokens:>9} {advisor:>8} {cost:>8} "
            f"{per_resolved:>10}  {reasons}"
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
