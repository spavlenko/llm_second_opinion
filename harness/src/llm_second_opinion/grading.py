"""Grade a patch in a fresh container from the task image, so the agent cannot alter the tests."""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task
from llm_second_opinion.testlogs import PARSERS, Outcome

# Bumped when grading changes what it decides; recorded in grade.json, so `bench regrade` can
# tell which grades are older. 1: before grade.json existed. 2: test-patch files are reset to
# the base commit before the test patch is applied, as SWE-bench does.
GRADER_VERSION = 2
# Printed by tasks/repos/common/run-tests when the build fails (it goes on to run ctest).
BUILD_FAILED = "run-tests: build failed"
BUILD_LOG = "/tmp/build.log"  # run-tests' full build output; it prints only the tail
BUILD_LOG_MAX = 2_000_000  # characters of it kept (the end)


@dataclass
class Grade:
    # resolved, empty_patch, patch_failed, test_patch_failed, build_failed, tests_failed, timeout
    reason: str
    resolved: bool
    log: str = field(default="", repr=False)
    # Per-test outcomes when the task has test lists; a listed test that is absent did not run.
    tests: dict[str, Outcome] = field(default_factory=dict)
    duration_s: float = 0.0
    build_failed: bool = False
    build_log: str | None = field(default=None, repr=False)  # the full log when the build failed
    f2p: dict[str, Any] | None = None  # {passed, total, failing: [...]}
    p2p: dict[str, Any] | None = None  # {passed, total, broken: [...]}
    missing: list[str] = field(default_factory=list)  # listed tests with no outcome
    # Files the agent's patch changed that the test patch also touches or that sit in a test
    # directory: the prompt tells the agent not to change existing tests.
    agent_touched_test_files: list[str] = field(default_factory=list)
    grader_version: int = GRADER_VERSION

    def record(self) -> dict[str, Any]:
        """grade.json: everything but the logs (grade.log and build.log hold those)."""
        return {k: v for k, v in asdict(self).items() if k not in ("log", "build_log")}

    @classmethod
    def from_record(cls, record: dict[str, Any], log: str = "") -> Grade:
        known = {k: v for k, v in record.items() if k in cls.__dataclass_fields__}
        return cls(**known, log=log)


def grade(box: Container, task: Task, patch: str, timeout_s: float) -> Grade:
    if not patch.strip():
        return Grade("empty_patch", False)
    graded = run_tests(box, task, [("patch_failed", patch)], timeout_s, reset_test_files=True)
    graded.agent_touched_test_files = touched_test_files(patch, task.test_patch)
    return graded


_DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def diff_files(diff: str) -> dict[str, str]:
    """Files a git diff touches (new paths) -> `added`, `deleted`, or `modified`."""
    files = {}
    blocks = _DIFF.split(diff)
    # split: [preamble, a1, b1, body1, a2, b2, body2, ...]
    for i in range(1, len(blocks) - 2, 3):
        body = blocks[i + 2]
        head = body.split("\n@@", 1)[0]
        if "\nnew file mode" in head or "\n--- /dev/null" in head:
            files[blocks[i + 1]] = "added"
        elif "\ndeleted file mode" in head or "\n+++ /dev/null" in head:
            files[blocks[i]] = "deleted"
        else:
            files[blocks[i + 1]] = "modified"
    return files


def touched_test_files(patch: str, test_patch: str) -> list[str]:
    tests = diff_files(test_patch)
    return sorted(
        f
        for f in diff_files(patch)
        if f in tests or any("test" in part.lower() for part in f.split("/")[:-1])
    )


def reset_commands(test_patch: str) -> list[str]:
    """Shell commands that put every file the test patch touches back to the base commit
    (the image's one-commit repository), as SWE-bench does: a file the test patch adds is
    removed, any other is checked out from HEAD. The agent's edits to them are discarded."""
    commands = []
    for path, kind in diff_files(test_patch).items():
        quoted = shlex.quote(path)
        if kind == "added":
            commands.append(f"rm -f -- {quoted}")
        else:
            commands.append(f"git checkout HEAD -- {quoted}")
    return commands


def run_tests(
    box: Container,
    task: Task,
    patches: list[tuple[str, str]],
    timeout_s: float,
    parser: str | None = None,
    reset_test_files: bool = False,
    test_patch: bool = True,
) -> Grade:
    """Apply `patches` (failure reason, diff) and then the test patch, and run `eval_command`.

    With `reset_test_files` (grading an agent's patch), the files the test patch touches are
    first put back to the base commit, so an agent's edits to them do not stop the test patch
    from applying (`reset_commands`). Validation calls this with no patch (the test patch
    alone) and with the gold patch, without the reset, and passes `parser` to get per-test
    outcomes before the task has test lists. Without `test_patch`, only the repository's own
    tests run, as on the agent's machine (the local picker).
    """
    steps = list(patches)
    if task.test_patch and test_patch:
        steps.append(("test_patch_failed", task.test_patch))
    log = []
    for i, (failure, content) in enumerate(steps):
        if failure == "test_patch_failed" and reset_test_files:
            for command in reset_commands(content):
                done = box.exec(command, workdir=task.workdir)
                log.append(f"$ {command}  # reset to base\n{done.output}")
                if done.exit_code != 0:
                    return Grade("test_patch_failed", False, "\n".join(log))
        path = f"/tmp/patch-{i}.diff"
        box.write(path, content)
        applied = box.exec(f"git apply --binary {path}", workdir=task.workdir)
        log.append(f"$ git apply {path}  # {failure.removesuffix('_failed')}\n{applied.output}")
        if applied.exit_code != 0:
            return Grade(failure, False, "\n".join(log))
    start = time.monotonic()
    run = box.exec(task.eval_command, workdir=task.workdir, timeout_s=timeout_s)
    duration = time.monotonic() - start
    log.append(f"$ {task.eval_command}\n{run.output}\n[exit {run.exit_code}]")
    build_failed = BUILD_FAILED in run.output
    build_log = _build_log(box) if build_failed else None
    if run.timed_out:
        return Grade("timeout", False, "\n".join(log), duration_s=duration,
                     build_failed=build_failed, build_log=build_log)  # fmt: skip
    parser = task.tests.parser if task.tests else parser
    outcomes = PARSERS[parser](run.output) if parser else {}
    graded = Grade("", False, "\n".join(log), outcomes, duration, build_failed, build_log)
    if task.tests is None:
        graded.resolved = run.exit_code == 0
    else:
        f2p, p2p = task.tests.fail_to_pass, task.tests.pass_to_pass
        graded.f2p = _tally(f2p, outcomes, "failing")
        graded.p2p = _tally(p2p, outcomes, "broken")
        graded.missing = [t for t in f2p + p2p if t not in outcomes]
        graded.resolved = graded.f2p["passed"] + graded.p2p["passed"] == len(f2p) + len(p2p)
    graded.reason = "resolved" if graded.resolved else _failure(build_failed)
    return graded


def _tally(tests: list[str], outcomes: dict[str, Outcome], name: str) -> dict[str, Any]:
    bad = [t for t in tests if outcomes.get(t) != "passed"]
    return {"passed": len(tests) - len(bad), "total": len(tests), name: bad}


def _failure(build_failed: bool) -> str:
    """Not resolved: `build_failed` when the build broke (tests whose targets did not build
    then fail as not run), else `tests_failed`."""
    return "build_failed" if build_failed else "tests_failed"


def _build_log(box: Container) -> str | None:
    """run-tests prints only the tail of a failed build; the whole log is still in the
    grading container."""
    try:
        text = box.read(BUILD_LOG)
    except Exception:  # noqa: BLE001 - a missing log must not fail the grade
        return None
    if text is not None and len(text) > BUILD_LOG_MAX:
        text = f"[... {len(text) - BUILD_LOG_MAX} characters omitted ...]\n" + text[-BUILD_LOG_MAX:]
    return text
