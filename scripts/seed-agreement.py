"""Cross-seed signals for `L-best3`: on tasks with >= 3 graded A0 seeds, does disagreement
between the seeds' patches predict failure, and which local rule picks the best patch?

    harness/.venv/bin/python scripts/seed-agreement.py SIGNALS.csv RUNS_DIR/<experiment> ...

SIGNALS.csv comes from scripts/uncertainty-signals.py (same experiments). Patches are compared
by character similarity of their added and removed lines; the hidden tests are never read.
"""

from __future__ import annotations

import csv
import difflib
import itertools
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def changed(diff: str) -> str:
    return "\n".join(
        x for x in diff.splitlines() if x[:1] in "+-" and not x.startswith(("+++", "---"))
    )


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def main() -> None:
    signals_csv, exp_dirs = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
    rows = {(r["experiment"], r["task"], r["seed"]): r for r in csv.DictReader(signals_csv.open())}
    for exp in exp_dirs:
        for grade in sorted((exp / "A0").glob("*/seed-*/*/attempt-*/grade.json")):
            item = json.loads((grade.parent / "item.json").read_text())
            key = (item["experiment"], item["task"], str(item["seed"]))
            if key in rows:
                rows[key]["patch"] = changed((grade.parent / "patch.diff").read_text())
    groups = defaultdict(list)
    for (exp, task, _), r in rows.items():
        if "patch" in r:
            groups[(exp, task)].append(r)
    groups = {
        k: sorted(v, key=lambda r: int(r["seed"]))[:3] for k, v in groups.items() if len(v) >= 3
    }
    print(f"{len(groups)} task groups with 3 seeds")

    # 1. disagreement vs the task's A0 rate
    table = []
    for (exp, task), g in groups.items():
        sims = [similarity(a["patch"], b["patch"]) for a, b in itertools.combinations(g, 2)]
        table.append((statistics.mean(sims), sum(int(r["resolved"]) for r in g), task))
    by_rate = defaultdict(list)
    for s, n, _ in table:
        by_rate[n].append(s)
    print("\nmean pairwise patch similarity by A0 resolved/3:")
    for n in sorted(by_rate):
        v = by_rate[n]
        print(f"  {n}/3  tasks {len(v):>3}  similarity {statistics.mean(v):.2f}")

    # 2. picking one of three, locally
    def medoid(g):
        return max(
            g, key=lambda r: sum(similarity(r["patch"], o["patch"]) for o in g if o is not r)
        )

    def verdict(r):  # tests run and passed at the end, non-empty patch
        return int(r["no_test_run"]) == 0 and int(r["last_test_failed"]) == 0 and r["patch"] != ""

    rules = {
        "random (expected)": None,
        "medoid patch": medoid,
        "least reasoning": lambda g: min(g, key=lambda r: float(r["reasoning_ktok"])),
        "smallest non-empty patch": lambda g: min(
            [r for r in g if r["patch"]] or g, key=lambda r: len(r["patch"])
        ),
        "own tests pass, then medoid": lambda g: medoid([r for r in g if verdict(r)] or g),
        "own tests pass, then least reasoning": lambda g: min(
            [r for r in g if verdict(r)] or g, key=lambda r: float(r["reasoning_ktok"])
        ),
    }
    oracle = sum(any(int(r["resolved"]) for r in g) for g in groups.values())
    mean = sum(sum(int(r["resolved"]) for r in g) / 3 for g in groups.values())
    print(f"\npick one of 3 on {len(groups)} tasks (resolved tasks):")
    print(f"  {'oracle (any seed resolves)':40} {oracle:>3}")
    for name, rule in rules.items():
        got = mean if rule is None else sum(int(rule(g)["resolved"]) for g in groups.values())
        print(f"  {name:40} {got:>5.1f}")


if __name__ == "__main__":
    main()
