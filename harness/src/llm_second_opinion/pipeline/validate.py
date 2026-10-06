"""Gold-patch validation on arm64, and freezing validated candidates into a manifest.

Each run tests the candidate twice in fresh containers: with the test patch alone ("before")
and with the gold patch as well ("after"). The task's test lists are re-derived from these
runs; upstream's lists come from x86-64 runs, where one test target failing to compile
marks the whole suite as failing.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from llm_second_opinion.grading import run_tests
from llm_second_opinion.pipeline.candidates import Candidate, CandidateSet
from llm_second_opinion.pipeline.images import RUN_TESTS
from llm_second_opinion.runtime import Runtime
from llm_second_opinion.tasks import Dropped, Manifest, Task, TestLists
from llm_second_opinion.testlogs import Outcome

Outcomes = dict[str, Outcome]


@dataclass
class Derived:
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    # Upstream pass-to-pass tests that do not pass with the gold patch here; left out.
    excluded: list[str]


class Rejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def derive(candidate: Candidate, before: Sequence[Outcomes], after: Sequence[Outcomes]) -> Derived:
    """Test lists from repeated before/after runs; raises Rejected.

    Every upstream fail-to-pass test must pass with the gold patch, every test must behave
    the same in every run, and at least one test must go from not passing to passing.
    """
    universe = sorted(set(candidate.fail_to_pass) | set(candidate.pass_to_pass))
    flaky = [
        t
        for t in universe
        if len({o.get(t) for o in before}) > 1 or len({o.get(t) for o in after}) > 1
    ]
    if flaky:
        raise Rejected("flaky", ", ".join(flaky))
    passes = [t for t in universe if after[0].get(t) == "passed"]
    unfixed = [t for t in candidate.fail_to_pass if t not in passes]
    if unfixed:
        raise Rejected("gold_does_not_pass", ", ".join(unfixed))
    f2p = [t for t in passes if before[0].get(t) != "passed"]
    if not f2p:
        raise Rejected("no_fail_to_pass", "every test passes with the test patch alone")
    return Derived(
        fail_to_pass=f2p,
        pass_to_pass=[t for t in passes if before[0].get(t) == "passed"],
        excluded=[t for t in candidate.pass_to_pass if t not in passes],
    )


def validate(
    candidate: Candidate,
    build: dict,
    runtime: Runtime,
    logs: Path,
    *,
    runs: int,
    timeout_s: float,
    cpus: float,
    memory_gb: float,
) -> dict:
    """Validation record for one built candidate: kept with test lists, or dropped."""
    task = _task(candidate, build, tests=None)
    before: list[Outcomes] = []
    after: list[Outcomes] = []
    test_s = 0.0
    logs.mkdir(parents=True, exist_ok=True)
    try:
        for run in range(1, runs + 1):
            for phase, patches, results in (
                ("before", [], before),
                ("after", [("gold_patch_failed", candidate.gold_patch)], after),
            ):
                box = runtime.start(build["image_id"], cpus, memory_gb, f"validate {task.id}")
                try:
                    graded = run_tests(box, task, patches, timeout_s, parser=build["parser"])
                finally:
                    box.remove()
                (logs / f"{phase}-{run}.log").write_text(graded.log)
                if graded.reason in ("gold_patch_failed", "test_patch_failed", "timeout"):
                    raise Rejected(graded.reason, f"{phase} run {run}; see {logs}")
                results.append(graded.tests)
                if phase == "after":
                    test_s = max(test_s, graded.duration_s)
        derived = derive(candidate, before, after)
    except Rejected as e:
        return {"status": "dropped", "reason": e.reason, "detail": e.detail}
    return {
        "status": "kept",
        "fail_to_pass": derived.fail_to_pass,
        "pass_to_pass": derived.pass_to_pass,
        "excluded_pass_to_pass": derived.excluded,
        "test_s": round(test_s, 1),
    }


def freeze(
    cset: CandidateSet,
    builds: dict[str, dict],
    validations: dict[str, dict],
    *,
    version: str,
    max_build_s: float,
    max_test_s: float,
    test_fraction: float,
    seed: int,
    keep_splits: dict[str, str] | None = None,
) -> Manifest:
    """The frozen manifest: every candidate is either a task or listed as dropped, with why.
    Tasks in `keep_splits` keep that split (an earlier version's); only the rest are split."""
    tasks, dropped = [], []
    for c in cset.candidates:
        b, v = builds.get(c.id), validations.get(c.id)
        if b is None or v is None:
            dropped.append(Dropped(id=c.id, reason="not_built" if b is None else "not_validated"))
        elif "error" in b:
            dropped.append(Dropped(id=c.id, reason=b["error"], detail=b.get("detail", "")))
        elif v["status"] != "kept":
            dropped.append(Dropped(id=c.id, reason=v["reason"], detail=v.get("detail", "")))
        elif b["build_s"] > max_build_s:
            dropped.append(Dropped(id=c.id, reason="build_too_slow", detail=f"{b['build_s']} s"))
        elif v["test_s"] > max_test_s:
            dropped.append(Dropped(id=c.id, reason="tests_too_slow", detail=f"{v['test_s']} s"))
        else:
            tests = TestLists(
                parser=b["parser"],
                fail_to_pass=v["fail_to_pass"],
                pass_to_pass=v["pass_to_pass"],
            )
            tasks.append(_task(c, b, tests))
    if not tasks:
        raise ValueError("no candidate passed validation")
    keep_splits = keep_splits or {}
    splits = keep_splits | split([t for t in tasks if t.id not in keep_splits], test_fraction, seed)
    tasks = [t.model_copy(update={"split": splits[t.id]}) for t in tasks]
    return Manifest(version=version, source=cset.source, tasks=tasks, dropped=dropped)


def split(tasks: Sequence[Task], test_fraction: float, seed: int) -> dict[str, str]:
    """`dev` or `test` per task: a fixed-seed shuffle within each repository, so both splits
    cover every repository (a repository with one task goes to `dev`)."""
    by_repo: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        by_repo[t.repo or ""].append(t.id)
    result = {}
    for repo, ids in sorted(by_repo.items()):
        ids = sorted(ids)
        random.Random(f"{seed}:{repo}").shuffle(ids)
        n_test = int(len(ids) * test_fraction + 0.5) if len(ids) > 1 else 0
        result |= {i: "test" for i in ids[:n_test]} | {i: "dev" for i in ids[n_test:]}
    return result


def smoke(manifest: Manifest, builds: dict[str, dict], validations: dict[str, dict], n: int):
    """The `n` quickest `dev` tasks (build plus test time), from different repositories."""
    ranked = sorted(
        (t for t in manifest.tasks if t.split == "dev"),
        key=lambda t: builds[t.id]["build_s"] + validations[t.id]["test_s"],
    )
    chosen, repos = [], set()
    for t in ranked:
        if t.repo not in repos:
            chosen.append(t)
            repos.add(t.repo)
        if len(chosen) == n:
            break
    return manifest.model_copy(
        update={"version": f"{manifest.version}-smoke", "tasks": chosen, "dropped": []}
    )


def _task(c: Candidate, build: dict, tests: TestLists | None) -> Task:
    return Task(
        id=c.id,
        image=build["image"],
        image_id=build["image_id"],
        problem_statement=c.problem_statement,
        test_patch=c.test_patch,
        gold_patch=c.gold_patch,
        eval_command=RUN_TESTS,
        tests=tests,
        repo=c.repo,
        base_commit=c.base_commit,
        base_date=build["base_date"],
    )
