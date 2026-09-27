"""Grade a patch in a fresh container from the task image, so the agent cannot alter the tests."""

from __future__ import annotations

from dataclasses import dataclass

from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task


@dataclass
class Grade:
    resolved: bool
    reason: str  # resolved, empty_patch, patch_failed, test_patch_failed, tests_failed, timeout
    log: str


def grade(box: Container, task: Task, patch: str, timeout_s: float) -> Grade:
    if not patch.strip():
        return Grade(False, "empty_patch", "")
    steps = [("patch_failed", "/tmp/agent.patch", patch)]
    if task.test_patch:
        steps.append(("test_patch_failed", "/tmp/test.patch", task.test_patch))
    log = []
    for failure, path, content in steps:
        box.write(path, content)
        applied = box.exec(f"git apply --binary {path}", workdir=task.workdir)
        log.append(f"$ git apply {path}\n{applied.output}")
        if applied.exit_code != 0:
            return Grade(False, failure, "\n".join(log))
    tests = box.exec(task.eval_command, workdir=task.workdir, timeout_s=timeout_s)
    log.append(f"$ {task.eval_command}\n{tests.output}\n[exit {tests.exit_code}]")
    if tests.timed_out:
        return Grade(False, "timeout", "\n".join(log))
    passed = tests.exit_code == 0
    return Grade(passed, "resolved" if passed else "tests_failed", "\n".join(log))
