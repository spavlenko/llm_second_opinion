"""`L-best3`: one of several local attempts, picked with what the agent's machine can see.

Each candidate patch gets a local check in a fresh task container: the patch applied, then
the repository's own tests (`run_tests` without the hidden test patch). The base commit gets
the same check once per task, so a candidate is judged by the visible tests it breaks. The
rules see `Visible` only: never the grade, which scores the pick afterwards.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_second_opinion.grading import run_tests
from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task

LOCAL_CHECK = "local-check.json"  # in the attempt directory
BASE_DIR = "local-base"  # runs/<experiment>/local-base/<task>.json


def local_check(box: Container, task: Task, patch: str, timeout_s: float) -> dict[str, Any]:
    """Apply `patch` (none: the base commit) and run the repository's own tests."""
    patches = [("patch_failed", patch)] if patch.strip() else []
    parser = task.tests.parser if task.tests else None
    g = run_tests(box, task, patches, timeout_s, parser=parser, test_patch=False)
    return {
        "applied": g.reason != "patch_failed",
        "build_failed": g.build_failed,
        "timed_out": g.reason == "timeout",
        "passed": sorted(t for t, o in g.tests.items() if o == "passed"),
        "failed": sorted(t for t, o in g.tests.items() if o != "passed"),
        "duration_s": round(g.duration_s, 1),
    }


@dataclass(frozen=True)
class Visible:
    """What a picker may use about one candidate."""

    seed: int
    empty: bool
    check: dict[str, Any]
    reasoning_tokens: int

    def broken(self, base: dict[str, Any]) -> int:
        """Tests that pass on the base commit and not with the patch (not run counts)."""
        return len(set(base["passed"]) - set(self.check["passed"]))

    def usable(self) -> bool:
        return not self.empty and self.check["applied"] and not self.check["timed_out"]


def _first_usable(cands: list[Visible]) -> list[Visible]:
    return [c for c in cands if c.usable()] or cands


def pick_local(cands: list[Visible], base: dict[str, Any]) -> Visible:
    """The primary rule, fixed before the gate's runs were seen: a usable patch that builds,
    with the fewest broken visible tests, then the least reasoning, then the lowest seed."""
    return min(
        _first_usable(cands),
        key=lambda c: (c.check["build_failed"], c.broken(base), c.reasoning_tokens, c.seed),
    )


def pick_least_reasoning(cands: list[Visible], base: dict[str, Any]) -> Visible:
    return min(_first_usable(cands), key=lambda c: (c.reasoning_tokens, c.seed))


def pick_builds_then_random(cands: list[Visible], base: dict[str, Any]) -> Visible:
    pool = _first_usable(cands)
    pool = [c for c in pool if not c.check["build_failed"]] or pool
    return random.Random(str([c.seed for c in cands])).choice(pool)


RULES = {
    "local": pick_local,
    "least reasoning": pick_least_reasoning,
    "builds, then random": pick_builds_then_random,
}


@dataclass(frozen=True)
class Group:
    """One `L-best3` item: a task's seeds group*s .. group*s+group-1, with what the pickers may
    see and, apart from it, the grades."""

    task: str
    s: int
    visible: list[Visible]
    patches: dict[int, str]  # by seed
    resolved: dict[int, bool]  # by seed; never shown to a picker
    base: dict[str, Any]


def groups(exp_dir: Path, rows: list[dict[str, Any]], group: int) -> list[Group]:
    """The complete groups of `rows` (done ledger rows of one arm and config hash). Groups with
    a missing seed or local check, or a task without its base check, are left out."""
    by_task: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        by_task[r["task"]][r["seed"]] = r
    out = []
    for task, seeds in sorted(by_task.items()):
        base_path = exp_dir / BASE_DIR / f"{task}.json"
        if not base_path.exists():
            continue
        base = json.loads(base_path.read_text())
        for s in range((max(seeds) + 1) // group):
            members = [seeds.get(group * s + i) for i in range(group)]
            dirs = [exp_dir / m["attempt_dir"] for m in members if m and m["attempt_dir"]]
            if len(dirs) < group or not all((d / LOCAL_CHECK).exists() for d in dirs):
                continue
            visible, patches, resolved = [], {}, {}
            for m, d in zip(members, dirs):
                seed = m["seed"]
                patches[seed] = (
                    (d / "patch.diff").read_text() if (d / "patch.diff").exists() else ""
                )
                check = json.loads((d / LOCAL_CHECK).read_text())
                tokens = m["executor_reasoning_tokens"] or 0
                visible.append(Visible(seed, not patches[seed].strip(), check, tokens))
                resolved[seed] = bool(m["resolved"])
            out.append(Group(task, s, visible, patches, resolved, base))
    return out


def compose(
    exp_dir: Path,
    rows: list[dict[str, Any]],
    group: int,
    reviews: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per complete group (`groups`): each rule's pick scored by its grade, the oracle
    (any candidate resolves) and the mean (a random pick's expected score). With `reviews`
    (by (task, s): a record with `pick_seed` and `consulted`), the review consult's pick too;
    groups without a record are left out then."""
    out = []
    for g in groups(exp_dir, rows, group):
        if reviews is not None and (g.task, g.s) not in reviews:
            continue
        row: dict[str, Any] = {
            "task": g.task,
            "seed": g.s,
            "candidates": "/".join(str(v.seed) for v in g.visible),
            "oracle": int(any(g.resolved.values())),
            "mean": sum(g.resolved.values()) / group,
        }
        for name, rule in RULES.items():
            picked = rule(g.visible, g.base)
            row[name] = int(g.resolved[picked.seed])
            row[f"{name} seed"] = picked.seed
        if reviews is not None:
            rec = reviews[(g.task, g.s)]
            row["review"] = int(g.resolved[rec["pick_seed"]])
            row["review seed"] = rec["pick_seed"]
            row["review consulted"] = int(rec["consulted"])
        out.append(row)
    return out
