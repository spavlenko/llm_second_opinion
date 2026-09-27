"""Grade a patch in a fresh container from the task image, so the agent cannot alter the tests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task
from llm_second_opinion.testlogs import PARSERS, Outcome


@dataclass
class Grade:
    resolved: bool
    reason: str  # resolved, empty_patch, patch_failed, test_patch_failed, tests_failed, timeout
    log: str
    # Per-test outcomes when the task has test lists; a listed test that is absent did not run.
    tests: dict[str, Outcome] = field(default_factory=dict)
    duration_s: float = 0.0


def grade(box: Container, task: Task, patch: str, timeout_s: float) -> Grade:
    if not patch.strip():
        return Grade(False, "empty_patch", "")
    return run_tests(box, task, [("patch_failed", patch)], timeout_s)


def run_tests(
    box: Container,
    task: Task,
    patches: list[tuple[str, str]],
    timeout_s: float,
    parser: str | None = None,
) -> Grade:
    """Apply `patches` (failure reason, diff) and then the test patch, and run `eval_command`.

    Validation calls this with no patch (the test patch alone) and with the gold patch, and
    passes `parser` to get per-test outcomes before the task has test lists.
    """
    steps = list(patches)
    if task.test_patch:
        steps.append(("test_patch_failed", task.test_patch))
    log = []
    for i, (failure, content) in enumerate(steps):
        path = f"/tmp/patch-{i}.diff"
        box.write(path, content)
        applied = box.exec(f"git apply --binary {path}", workdir=task.workdir)
        log.append(f"$ git apply {path}  # {failure.removesuffix('_failed')}\n{applied.output}")
        if applied.exit_code != 0:
            return Grade(False, failure, "\n".join(log))
    start = time.monotonic()
    run = box.exec(task.eval_command, workdir=task.workdir, timeout_s=timeout_s)
    duration = time.monotonic() - start
    log.append(f"$ {task.eval_command}\n{run.output}\n[exit {run.exit_code}]")
    if run.timed_out:
        return Grade(False, "timeout", "\n".join(log), duration_s=duration)
    parser = task.tests.parser if task.tests else parser
    outcomes = PARSERS[parser](run.output) if parser else {}
    if task.tests is None:
        passed = run.exit_code == 0
        return Grade(passed, _reason(passed), "\n".join(log), outcomes, duration)
    listed = task.tests.fail_to_pass + task.tests.pass_to_pass
    passed = all(outcomes.get(t) == "passed" for t in listed)
    return Grade(passed, _reason(passed), "\n".join(log), outcomes, duration)


def _reason(passed: bool) -> str:
    return "resolved" if passed else "tests_failed"
