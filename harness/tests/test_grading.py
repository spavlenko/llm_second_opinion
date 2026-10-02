"""Grading detail without Docker; test_docker.py grades in real containers."""

from llm_second_opinion.grading import (
    GRADER_VERSION,
    Grade,
    diff_files,
    grade,
    reset_commands,
    touched_test_files,
)
from llm_second_opinion.runtime import ExecResult
from llm_second_opinion.tasks import Task, TestLists

PATCH = """\
diff --git a/src/a.cpp b/src/a.cpp
index 1..2 100644
--- a/src/a.cpp
+++ b/src/a.cpp
@@ -1 +1 @@
-x
+y
diff --git a/test/old.cpp b/test/old.cpp
index 1..2 100644
--- a/test/old.cpp
+++ b/test/old.cpp
@@ -1 +1 @@
-x
+y
diff --git a/projects/SelfTest/b.tests.cpp b/projects/SelfTest/b.tests.cpp
index 1..2 100644
--- a/projects/SelfTest/b.tests.cpp
+++ b/projects/SelfTest/b.tests.cpp
@@ -1 +1 @@
-x
+y
"""
TEST_PATCH = """\
diff --git a/test/new.cpp b/test/new.cpp
new file mode 100644
index 0000000..1
--- /dev/null
+++ b/test/new.cpp
@@ -0,0 +1 @@
+t
diff --git a/test/old.cpp b/test/old.cpp
index 1..3 100644
--- a/test/old.cpp
+++ b/test/old.cpp
@@ -1 +1 @@
-x
+z
"""


def ctest(**outcomes):
    lines = [
        f"  {i}/9 Test  #{i}: {name} ....   {'Passed' if ok else '***Failed'}    0.01 sec"
        for i, (name, ok) in enumerate(outcomes.items(), 1)
    ]
    return "\n".join(lines) + "\n"


class Box:
    """Answers every command with exit 0, and the eval command with a scripted log."""

    def __init__(self, log, files=None):
        self.log, self.files, self.commands = log, dict(files or {}), []

    def write(self, path, data):
        self.files[path] = data

    def read(self, path):
        return self.files.get(path)

    def exec(self, command, workdir=None, timeout_s=None, env=None):
        self.commands.append(command)
        return ExecResult(1, self.log) if command == "run" else ExecResult(0, "")


def task(f2p=("new",), p2p=("old", "other")):
    lists = TestLists(parser="ctest", fail_to_pass=list(f2p), pass_to_pass=list(p2p))
    return Task(id="t", image="i", problem_statement="p", eval_command="run",
                test_patch=TEST_PATCH, tests=lists)  # fmt: skip


def test_diff_files_and_test_file_edits():
    assert diff_files(TEST_PATCH) == {"test/new.cpp": "added", "test/old.cpp": "modified"}
    assert touched_test_files(PATCH, TEST_PATCH) == [
        "projects/SelfTest/b.tests.cpp",
        "test/old.cpp",
    ]


def test_test_patch_files_are_reset_to_base_before_the_test_patch():
    assert reset_commands(TEST_PATCH) == [
        "rm -f -- test/new.cpp",
        "git checkout HEAD -- test/old.cpp",
    ]
    box = Box(ctest(new=True, old=True, other=True))
    graded = grade(box, task(), PATCH, 60)
    applies = [c for c in box.commands if c.startswith(("git apply", "rm", "git checkout"))]
    assert applies == [
        "git apply --binary /tmp/patch-0.diff",
        "rm -f -- test/new.cpp",
        "git checkout HEAD -- test/old.cpp",
        "git apply --binary /tmp/patch-1.diff",
    ]
    assert graded.resolved and graded.agent_touched_test_files == [
        "projects/SelfTest/b.tests.cpp",
        "test/old.cpp",
    ]


def test_grade_record_counts_f2p_and_p2p():
    graded = grade(Box(ctest(new=False, old=True)), task(), PATCH, 60)
    assert (graded.reason, graded.resolved, graded.build_failed) == ("tests_failed", False, False)
    assert graded.f2p == {"passed": 0, "total": 1, "failing": ["new"]}
    assert graded.p2p == {"passed": 1, "total": 2, "broken": ["other"]}
    assert graded.missing == ["other"]
    record = graded.record()
    assert "log" not in record and record["grader_version"] == GRADER_VERSION
    assert set(record) >= {"reason", "build_failed", "tests", "f2p", "p2p", "missing", "duration_s"}
    assert Grade.from_record(record).f2p == graded.f2p


def test_a_failed_build_is_its_own_reason_and_keeps_the_whole_log():
    log = "run-tests: build failed; last lines of /tmp/build.log:\nerror\n" + ctest(old=True)
    box = Box(log, {"/tmp/build.log": "line\n" * 500 + "error: x\n"})
    graded = grade(box, task(), PATCH, 60)
    assert (graded.reason, graded.build_failed) == ("build_failed", True)
    assert graded.build_log.count("line") == 500
