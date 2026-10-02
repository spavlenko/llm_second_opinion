"""Per-arm summaries and paired comparisons from the ledger, for the terminal and as CSV."""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from llm_second_opinion.config import ADVISOR_MODEL, ConfigError, Experiment

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


# --- Paired comparisons --------------------------------------------------------

RESAMPLES = 10_000
BOOTSTRAP_SEED = 0  # fixed, so a report is reproducible
PAIR_COLUMNS = [
    "arm", "baseline", "tasks", "pairs", "rate", "baseline_rate", "diff", "diff_ci_low",
    "diff_ci_high", "wins", "losses", "mcnemar_p", "rel_lift", "ceiling", "gap_pairs",
    "gap_closed", "gap_ci_low", "gap_ci_high",
]  # fmt: skip


@dataclass
class Paired:
    """One arm against the baseline on the (task, seed) items both completed."""

    arm: str
    baseline: str
    tasks: int = 0
    pairs: int = 0
    rate: float | None = None  # the arm's resolve rate on the paired items
    baseline_rate: float | None = None
    diff: float | None = None  # rate - baseline_rate
    diff_ci_low: float | None = None
    diff_ci_high: float | None = None
    wins: int = 0  # resolved by the arm only
    losses: int = 0  # resolved by the baseline only
    mcnemar_p: float | None = None
    rel_lift: float | None = None  # diff / baseline_rate
    ceiling: str | None = None
    gap_pairs: int = 0  # items all three arms completed
    gap_closed: float | None = None  # (arm - baseline) / (ceiling - baseline)
    gap_ci_low: float | None = None
    gap_ci_high: float | None = None


def outcomes(exp: Experiment, rows: list[dict[str, Any]]) -> dict[str, dict[tuple, bool]]:
    """Resolved or not per arm and (task, seed), for done items at the current config."""
    by_arm: dict[str, dict[tuple, bool]] = defaultdict(dict)
    for r in current_rows(exp, rows):
        if r["status"] == "done":
            by_arm[r["arm"]][(r["task"], r["seed"])] = bool(r["resolved"])
    return by_arm


def paired_comparisons(
    exp: Experiment,
    rows: list[dict[str, Any]],
    baseline: str | None = None,
    ceiling: str | None = None,
    resamples: int = RESAMPLES,
) -> list[Paired]:
    """Each arm against `baseline` (default A0 when present) on shared items.

    The interval is a paired bootstrap over tasks: tasks are resampled with replacement and
    a task's seeds stay together, since seeds of one task are not independent. McNemar's
    exact test uses the discordant (task, seed) pairs. With a `ceiling` arm (default A4 when
    present), the share of the baseline-ceiling gap closed is computed on the items all three
    completed. Empty when there is no baseline arm.
    """
    names = [a.name for a in exp.arms]
    for name in (baseline, ceiling):
        if name is not None and name not in names:
            raise ConfigError(f"no arm named {name!r}; arms: {', '.join(names)}")
    baseline = baseline or ("A0" if "A0" in names else None)
    ceiling = ceiling or ("A4" if "A4" in names else None)
    if baseline is None:
        return []
    results = outcomes(exp, rows)
    base = results.get(baseline, {})
    top = results.get(ceiling, {}) if ceiling and ceiling != baseline else None
    out = []
    for arm in names:
        if arm == baseline:
            continue
        mine = results.get(arm, {})
        keys = sorted(mine.keys() & base.keys())
        p = Paired(arm, baseline, ceiling=ceiling if top is not None and arm != ceiling else None)
        if keys:
            _fill_diff(p, keys, mine, base, resamples)
        if p.ceiling and top is not None:
            triples = sorted(mine.keys() & base.keys() & top.keys())
            if triples:
                _fill_gap(p, triples, mine, base, top, resamples)
        out.append(p)
    return out


def _by_task(keys: list[tuple], *arms: dict[tuple, bool]) -> list[tuple[int, ...]]:
    """Per task: (items, resolved by each arm...), the unit the bootstrap resamples."""
    sums: dict[str, list[int]] = {}
    for key in keys:
        row = sums.setdefault(key[0], [0] * (len(arms) + 1))
        row[0] += 1
        for i, arm in enumerate(arms, 1):
            row[i] += arm[key]
    return [tuple(v) for v in sums.values()]


def _fill_diff(p: Paired, keys: list[tuple], mine: dict, base: dict, resamples: int) -> None:
    groups = _by_task(keys, mine, base)

    def diff(t: Sequence[int]) -> float:
        return (t[1] - t[2]) / t[0]

    total = _total(groups)
    p.tasks, p.pairs = len(groups), len(keys)
    p.rate, p.baseline_rate = total[1] / total[0], total[2] / total[0]
    p.diff = diff(total)
    p.diff_ci_low, p.diff_ci_high = bootstrap_ci(groups, diff, resamples) or (None, None)
    p.wins = sum(mine[k] and not base[k] for k in keys)
    p.losses = sum(base[k] and not mine[k] for k in keys)
    p.mcnemar_p = mcnemar_exact(p.wins, p.losses)
    p.rel_lift = p.diff / p.baseline_rate if p.baseline_rate else None


def _fill_gap(
    p: Paired, keys: list[tuple], mine: dict, base: dict, top: dict, resamples: int
) -> None:
    groups = _by_task(keys, mine, base, top)

    def gap(t: Sequence[int]) -> float | None:
        return (t[1] - t[2]) / (t[3] - t[2]) if t[3] > t[2] else None

    p.gap_pairs = len(keys)
    p.gap_closed = gap(_total(groups))
    if p.gap_closed is not None:
        p.gap_ci_low, p.gap_ci_high = bootstrap_ci(groups, gap, resamples) or (None, None)


def _total(groups: Sequence[tuple[int, ...]]) -> tuple[int, ...]:
    return tuple(map(sum, zip(*groups)))


def bootstrap_ci(
    groups: list[tuple[int, ...]],
    stat: Callable[[Sequence[int]], float | None],
    resamples: int = RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Percentile 95% interval of `stat` over the summed groups, resampling groups (tasks)
    with replacement. Resamples where `stat` is undefined (None) are skipped; None when fewer
    than half are defined."""
    rng = random.Random(seed)
    n = len(groups)
    values = []
    for _ in range(resamples):
        value = stat(_total(rng.choices(groups, k=n)))
        if value is not None:
            values.append(value)
    if len(values) < max(2, resamples / 2):
        return None
    cuts = statistics.quantiles(values, n=40, method="inclusive")  # 2.5% steps
    return cuts[0], cuts[-1]


def mcnemar_exact(wins: int, losses: int) -> float:
    """Two-sided exact McNemar p-value: a binomial test on the discordant pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_metrics(p: Paired) -> dict[str, float]:
    """MLflow metrics for an arm's run; undefined values are left out."""
    names = {
        "pairs": "paired_items",
        "diff": "paired_diff",
        "diff_ci_low": "paired_diff_ci_low",
        "diff_ci_high": "paired_diff_ci_high",
        "mcnemar_p": "paired_mcnemar_p",
        "rel_lift": "paired_rel_lift",
        "gap_closed": "gap_closed",
        "gap_ci_low": "gap_closed_ci_low",
        "gap_ci_high": "gap_closed_ci_high",
    }
    values = asdict(p)
    return {m: float(values[k]) for k, m in names.items() if values[k] is not None}


def format_pairs(pairs: list[Paired]) -> str:
    if not pairs:
        return "paired comparisons: none (needs a baseline arm, A0 or --baseline, and another arm)"
    base, ceiling = pairs[0].baseline, next((p.ceiling for p in pairs if p.ceiling), None)
    title = f"paired vs {base}: same task and seed; 95% bootstrap CI over tasks"
    header = f"{'arm':<12} {'pairs':>5} {'tasks':>5} {'rate':>5} {base[:7]:>7} {'diff':>6} "
    header += f"{'95% CI':>13} {'+/-':>7} {'McNemar p':>9} {'rel lift':>8}"
    if ceiling:
        header += f"  {'gap closed (' + ceiling + ')':>16} {'95% CI':>13}"
    lines = [title, header, "-" * len(header)]
    for p in pairs:
        if not p.pairs:
            lines.append(f"{p.arm:<12} {0:>5}  no items completed by both arms")
            continue
        line = (
            f"{p.arm:<12} {p.pairs:>5} {p.tasks:>5} {_pct(p.rate):>5} {_pct(p.baseline_rate):>7} "
            f"{_pp(p.diff):>6} {_ci(p.diff_ci_low, p.diff_ci_high, _pp):>13} "
            f"{f'{p.wins}/{p.losses}':>7} {p.mcnemar_p:>9.3f} {_pct(p.rel_lift, sign=True):>8}"
        )
        if ceiling:
            gap = _pct(p.gap_closed) if p.gap_pairs else "-"
            line += f"  {gap:>16} {_ci(p.gap_ci_low, p.gap_ci_high, _pct):>13}"
        lines.append(line)
    lines.append("+/- = items resolved by the arm only / by the baseline only; rates are on the")
    lines.append("paired items. Gap closed = (arm - baseline) / (ceiling - baseline), on items all")
    lines.append("three completed; undefined when the ceiling is not above the baseline.")
    return "\n".join(lines)


def write_pairs_csv(pairs: list[Paired], path: Path) -> None:
    """One row per arm compared with the baseline."""
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, PAIR_COLUMNS)
        writer.writeheader()
        writer.writerows(asdict(p) for p in pairs)


def _pct(value: float | None, sign: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:+.0%}" if sign else f"{value:.0%}"


def _pp(value: float | None) -> str:
    """A difference of rates, in percentage points."""
    return "-" if value is None else f"{value * 100:+.0f}pp"


def _ci(low: float | None, high: float | None, fmt: Callable[[float | None], str]) -> str:
    return "-" if low is None or high is None else f"{fmt(low)}..{fmt(high)}"


# --- Cost: Pareto front of resolve rate against advisor cost -------------------

_TOKENS = ("advisor_prompt_tokens", "advisor_completion_tokens")
_EXECUTOR_TOKENS = ("executor_prompt_tokens", "executor_completion_tokens")


@dataclass
class CostPoint:
    arm: str
    done: int
    rate: float
    cost: float | None  # mean advisor cost per item; None if not measured for every item
    cost_per_resolved: float | None
    on_front: bool = False


def pareto(exp: Experiment, rows: list[dict[str, Any]]) -> tuple[str, list[CostPoint]] | None:
    """Resolve rate against advisor cost per item, and which arms are on the Pareto front
    (no other arm resolves at least as often for no more cost, and better in one).

    Cost is `cost_usd` when the ledger has it, else advisor tokens (prompt + completion; for
    an arm whose executor is the advisor model, its executor tokens too). An arm that never
    calls the advisor model costs 0. None when the ledger has no cost data.
    """
    done = [r for r in current_rows(exp, rows) if r["status"] == "done"]
    if any(r.get("cost_usd") is not None for r in done):
        unit, columns, cloud_columns = "USD", ("cost_usd",), ("cost_usd",)
    elif any(r.get(c) is not None for r in done for c in _TOKENS):
        unit, columns, cloud_columns = "advisor tokens", _TOKENS, _TOKENS + _EXECUTOR_TOKENS
    else:
        return None
    points = []
    for arm in exp.arms:
        items = [r for r in done if r["arm"] == arm.name]
        if not items:
            continue
        cloud_executor = arm.executor == ADVISOR_MODEL
        if arm.advisor is None and not cloud_executor:
            costs: list[float | None] = [0.0] * len(items)
        else:
            costs = [_cost(r, cloud_columns if cloud_executor else columns) for r in items]
        resolved = sum(bool(r["resolved"]) for r in items)
        total = None if None in costs else sum(costs)
        points.append(
            CostPoint(
                arm.name,
                len(items),
                resolved / len(items),
                None if total is None else total / len(items),
                None if total is None or not resolved else total / resolved,
            )
        )
    measured = [p for p in points if p.cost is not None]
    for p in measured:
        p.on_front = not any(
            q.rate >= p.rate and q.cost <= p.cost and (q.rate > p.rate or q.cost < p.cost)
            for q in measured
        )
    return unit, points


def _cost(row: dict[str, Any], columns: tuple[str, ...]) -> float | None:
    values = [row.get(c) for c in columns]
    return None if None in values else float(sum(values))


def format_pareto(front: tuple[str, list[CostPoint]] | None) -> str:
    if front is None:
        return "Pareto front (resolve rate vs advisor cost): no cost data in the ledger"
    unit, points = front
    header = f"{'arm':<12} {'done':>5} {'rate':>5} {'cost/item':>12} {'cost/resolved':>14}  front"
    lines = [f"Pareto front: resolve rate vs advisor cost ({unit})", header, "-" * len(header)]
    for p in points:
        lines.append(
            f"{p.arm:<12} {p.done:>5} {p.rate:>5.0%} {_num(p.cost):>12} "
            f"{_num(p.cost_per_resolved):>14}  {'*' if p.on_front else ''}"
        )
    lines.append("* = on the front; - = not measured for every item")
    return "\n".join(lines)


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}" if value < 10 else f"{value:,.0f}"


# --- Honesty: variants tried and final runs --------------------------------------


def provenance(exp: Experiment, rows: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> str:
    """How many variants were tried (every config hash in the ledger, stale ones included:
    each prompt edit is a new hash) and whether test tasks were run with --final."""
    variants = len({r["config_hash"] for r in rows})
    lines = [
        (
            f"split: {exp.split or 'all tasks'}; arms in the config: {len(exp.arms)}; "
            f"variants tried (distinct config hashes in the ledger): {variants}"
        )
    ]
    final = [s for s in sessions if s["final"]]
    unguarded = [s for s in sessions if s["test_tasks"] and not s["final"]]
    if final:
        lines.append(f"final: {len(final)} batch(es) run with --final on test tasks")
    if unguarded:
        lines.append(f"WARNING: {len(unguarded)} batch(es) ran test tasks without --final")
    return "\n".join(lines)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
