"""Task pipeline without Docker: log parsing, import, recipes, derivation, split, and freezing."""

import json
import subprocess

import pytest

from llm_second_opinion.grading import grade
from llm_second_opinion.pipeline import multi_swe_bench
from llm_second_opinion.pipeline.candidates import Candidate, CandidateSet, Records
from llm_second_opinion.pipeline.images import checkout, ensure_commit, load_recipe
from llm_second_opinion.pipeline.multi_swe_bench import Dataset, import_candidates, to_candidate
from llm_second_opinion.pipeline.validate import Rejected, derive, freeze, split, validate
from llm_second_opinion.runtime import ExecResult
from llm_second_opinion.tasks import Manifest, Task, TestLists
from llm_second_opinion.testlogs import ctest

CTEST_LOG = """\
Test project /build
      Start  1: test-a
 1/4 Test  #1: test-a ...........................   Passed    0.05 sec
 2/4 Test  #2: test-b ...........................***Failed    1.20 sec
 3/4 Test  #3: Test-C ...........................***Not Run   0.00 sec
 4/4 Test  #4: test-d ...........................***Exception: SegFault  0.1 sec
50% tests passed, 2 tests failed out of 4
"""


def ctest_log(**outcomes: str) -> str:
    status = {"passed": "   Passed", "failed": "***Failed"}
    lines = [
        f"{i}/{len(outcomes)} Test #{i}: {name} .....{status[o]}    0.01 sec"
        for i, (name, o) in enumerate(outcomes.items(), 1)
    ]
    return "\n".join(lines)


def test_ctest_parser():
    assert ctest(CTEST_LOG) == {
        "test-a": "passed",
        "test-b": "failed",
        "Test-C": "failed",
        "test-d": "failed",
    }
    assert "test-c" in ctest(CTEST_LOG, lower=True)


def row(**over):
    base = {
        "org": "o",
        "repo": "r",
        "number": 7,
        "instance_id": "o__r-7",
        "base": {"sha": "abc"},
        "resolved_issues": [{"title": "Crash", "body": "It crashes.\r\nOften."}],
        "fix_patch": "fix",
        "test_patch": "test",
        "f2p_tests": {"t1": {}},
        "s2p_tests": {},
        "n2p_tests": {"t2": {}},
        "p2p_tests": {"t3": {}},
        "difficulty": "easy",
        "language": "c++",
    }
    return base | over


def test_to_candidate_merges_fixed_tests_and_uses_issue_text():
    c = to_candidate(row())
    assert (c.id, c.repo, c.number, c.base_commit) == ("o__r-7", "o/r", 7, "abc")
    assert c.fail_to_pass == ["t1", "t2"] and c.pass_to_pass == ["t3"]
    assert c.problem_statement == "Crash\n\nIt crashes.\nOften."


def test_import_filters_language_and_instances(tmp_path, monkeypatch):
    ds = Dataset("x/y", "rev", {"c++": ("data.jsonl",)}, {})
    monkeypatch.setitem(multi_swe_bench.DATASETS, "fake", ds)
    path = tmp_path / "x/y/rev/data.jsonl"
    path.parent.mkdir(parents=True)
    rows = [row(), row(instance_id="o__r-8", number=8), row(instance_id="p-1", language="go")]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert [c.id for c in import_candidates("fake", "c++", tmp_path).candidates] == [
        "o__r-7",
        "o__r-8",
    ]
    assert [c.id for c in import_candidates("fake", "c++", tmp_path, {"o__r-8"}).candidates] == [
        "o__r-8"
    ]
    with pytest.raises(ValueError, match="not in fake"):
        import_candidates("fake", "c++", tmp_path, {"p-1"})


@pytest.mark.parametrize(
    ("name", "number", "gcc"),
    [
        ("nlohmann/json", 2576, "7"),
        ("nlohmann/json", 2600, "14"),
        ("nlohmann/json", 3000, "12"),
        ("nlohmann/json", 4536, "14"),
        ("catchorg/Catch2", 1608, "7"),
        ("catchorg/Catch2", 2288, "12"),
        ("simdjson/simdjson", 958, "7"),
        ("simdjson/simdjson", 2016, "11"),
        ("fmtlib/fmt", 1171, "14"),
    ],
)
def test_recipes_pick_gcc_by_pr(repo, name, number, gcc):
    recipe = load_recipe(repo / "tasks/repos", name)
    assert recipe is not None and recipe.gcc_for(number) == gcc


def test_missing_recipe(tmp_path):
    assert load_recipe(tmp_path, "yhirose/cpp-httplib") is None


def candidate(**over) -> Candidate:
    base = {
        "id": "o__r-7",
        "repo": "o/r",
        "number": 7,
        "base_commit": "abc",
        "problem_statement": "p",
        "test_patch": "test",
        "gold_patch": "gold",
        "fail_to_pass": ["new"],
        "pass_to_pass": ["old", "x86only"],
    }
    return Candidate.model_validate(base | over)


P, F = "passed", "failed"


def test_derive_rederives_lists():
    # Upstream calls `old` pass-to-pass, and `also` would count as fixed only here: on x86
    # the whole suite failed to build with the test patch.
    c = candidate(fail_to_pass=["new", "also"])
    before = [{"old": P, "also": P}] * 2
    after = [{"new": P, "old": P, "also": P, "x86only": F}] * 2
    d = derive(c, before, after)
    assert d.fail_to_pass == ["new"]
    assert d.pass_to_pass == ["also", "old"]
    assert d.excluded == ["x86only"]


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        ([{}, {}], [{"new": P}, {"new": F}], "flaky"),
        ([{"old": P}, {"old": F}], [{"new": P}] * 2, "flaky"),
        ([{}] * 2, [{"new": F}] * 2, "gold_does_not_pass"),
        ([{"new": P}] * 2, [{"new": P}] * 2, "no_fail_to_pass"),
    ],
)
def test_derive_rejects(before, after, reason):
    with pytest.raises(Rejected) as e:
        derive(candidate(), before, after)
    assert e.value.reason == reason


class ScriptedBox:
    """Answers `git apply` and returns a scripted ctest log for the test command."""

    def __init__(self, log: str, apply_code: int = 0):
        self.log, self.apply_code, self.files = log, apply_code, {}

    def write(self, path, data):
        self.files[path] = data

    def exec(self, command, workdir=None, timeout_s=None, env=None):
        if command.startswith("git apply"):
            return ExecResult(self.apply_code, "")
        return ExecResult(8, self.log)

    def remove(self):
        pass


def task(tests: TestLists | None = None) -> Task:
    return Task(
        id="t", image="i", problem_statement="p", eval_command="run", test_patch="tp", tests=tests
    )


def test_grading_with_test_lists_ignores_exit_code():
    lists = TestLists(parser="ctest", fail_to_pass=["new"], pass_to_pass=["old"])
    ok = grade(ScriptedBox(ctest_log(new=P, old=P, other=F)), task(lists), "p", 60)
    assert ok.resolved and ok.tests["other"] == "failed"
    broken = grade(ScriptedBox(ctest_log(new=P, old=F)), task(lists), "p", 60)
    assert (broken.resolved, broken.reason) == (False, "tests_failed")
    missing = grade(ScriptedBox(ctest_log(old=P)), task(lists), "p", 60)
    assert not missing.resolved


def test_unknown_parser_is_rejected():
    with pytest.raises(ValueError, match="unknown parser"):
        TestLists(parser="junit", fail_to_pass=["a"])


class ScriptedRuntime:
    """Containers whose test output depends on whether the gold patch was written."""

    def __init__(self, before: str, after: str):
        self.before, self.after = before, after

    def start(self, image, cpus, memory_gb, name):
        runtime = self

        class Box(ScriptedBox):
            def exec(self, command, workdir=None, timeout_s=None, env=None):
                if command.startswith("git apply"):
                    return ExecResult(0, "")
                gold = "gold" in self.files.values()
                return ExecResult(8, runtime.after if gold else runtime.before)

        return Box("")


BUILD = {
    "image": "img:1",
    "image_id": "sha256:1",
    "parser": "ctest",
    "base_date": "2020-01-01T00:00:00Z",
    "build_s": 100.0,
}


def test_validate_keeps_and_records_lists(tmp_path):
    runtime = ScriptedRuntime(ctest_log(new=F, old=P), ctest_log(new=P, old=P, x86only=F))
    record = validate(
        candidate(), BUILD, runtime, tmp_path, runs=2, timeout_s=60, cpus=1, memory_gb=1
    )
    assert record["status"] == "kept"
    assert (record["fail_to_pass"], record["pass_to_pass"]) == (["new"], ["old"])
    assert record["excluded_pass_to_pass"] == ["x86only"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "after-1.log",
        "after-2.log",
        "before-1.log",
        "before-2.log",
    ]


def test_validate_drops_when_gold_does_not_apply(tmp_path):
    class GoldConflicts(ScriptedBox):
        def exec(self, command, workdir=None, timeout_s=None, env=None):
            path = command.split()[-1]
            if command.startswith("git apply") and self.files[path] == "gold":
                return ExecResult(1, "error: patch does not apply")
            return super().exec(command, workdir, timeout_s, env)

    class Runtime(ScriptedRuntime):
        def start(self, image, cpus, memory_gb, name):
            return GoldConflicts(ctest_log(new=F))

    record = validate(
        candidate(), BUILD, Runtime("", ""), tmp_path, runs=2, timeout_s=60, cpus=1, memory_gb=1
    )
    assert (record["status"], record["reason"]) == ("dropped", "gold_patch_failed")


def test_split_is_stratified_and_deterministic():
    tasks = [
        task().model_copy(update={"id": f"{repo}-{i}", "repo": repo})
        for repo, n in (("a", 10), ("b", 4), ("c", 1))
        for i in range(n)
    ]
    s = split(tasks, 0.5, seed=0)
    assert s == split(tasks, 0.5, seed=0)
    assert s != split(tasks, 0.5, seed=1)
    for repo, n_test in (("a", 5), ("b", 2), ("c", 0)):
        assert sum(v == "test" for k, v in s.items() if k.startswith(f"{repo}-")) == n_test


def test_freeze_lists_every_candidate(tmp_path):
    ids = ["ok-1", "ok-2", "unbuilt", "broken", "rejected", "slow"]
    cset = CandidateSet(source="s", candidates=[candidate(id=i) for i in ids])
    builds = {i: BUILD for i in ids if i != "unbuilt"} | {"broken": {"error": "build_failed"}}
    kept = {"status": "kept", "fail_to_pass": ["new"], "pass_to_pass": [], "test_s": 60.0}
    validations = {i: kept for i in ("ok-1", "ok-2", "broken")}
    validations["rejected"] = {"status": "dropped", "reason": "flaky", "detail": "new"}
    validations["slow"] = kept | {"test_s": 900.0}
    m = freeze(
        cset,
        builds,
        validations,
        version="v1",
        max_build_s=1200,
        max_test_s=600,
        test_fraction=0.5,
        seed=0,
    )
    assert [t.id for t in m.tasks] == ["ok-1", "ok-2"]
    assert {t.split for t in m.tasks} == {"dev", "test"}
    assert {d.id: d.reason for d in m.dropped} == {
        "unbuilt": "not_built",
        "broken": "build_failed",
        "rejected": "flaky",
        "slow": "tests_too_slow",
    }
    assert m.tasks[0].image_id == "sha256:1" and m.tasks[0].tests.fail_to_pass == ["new"]
    path = tmp_path / "m.yaml"
    m.to_yaml(path, "# header\n")
    assert Manifest.from_yaml(path) == m
    assert path.read_text().startswith("# header\n")


def test_freeze_keeps_earlier_splits_and_splits_only_new_tasks():
    ids = [f"r-{i}" for i in range(6)]
    cset = CandidateSet(source="s", candidates=[candidate(id=i) for i in ids])
    kept = {"status": "kept", "fail_to_pass": ["new"], "pass_to_pass": [], "test_s": 60.0}
    earlier = {"r-0": "test", "r-1": "test", "r-2": "test"}
    m = freeze(
        cset,
        dict.fromkeys(ids, BUILD),
        dict.fromkeys(ids, kept),
        version="v2",
        max_build_s=1200,
        max_test_s=600,
        test_fraction=0.5,
        seed=0,
        keep_splits=earlier,
    )
    splits = {t.id: t.split for t in m.tasks}
    assert {i: splits[i] for i in earlier} == earlier
    assert sorted(splits[i] for i in ("r-3", "r-4", "r-5")) == ["dev", "test", "test"]


def test_records_persist(tmp_path):
    Records(tmp_path / "r.json").put("a", {"x": 1})
    assert Records(tmp_path / "r.json").get("a") == {"x": 1}


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always"]
        + list(args),
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_checkout_includes_submodules_recursively(tmp_path):
    repos = {}
    for name in ("leaf", "dep", "main"):
        repos[name] = tmp_path / name
        repos[name].mkdir()
        git(repos[name], "init", "-q", "-b", "main")
        (repos[name] / f"{name}.txt").write_text(name)
        git(repos[name], "add", ".")
        git(repos[name], "commit", "-qm", name)
    git(repos["dep"], "submodule", "add", "-q", str(repos["leaf"]), "third/leaf")
    git(repos["dep"], "commit", "-qm", "add leaf")
    git(repos["main"], "submodule", "add", "-q", str(repos["dep"]), "deps/dep")
    git(repos["main"], "commit", "-qm", "add dep")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repos["main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    mirrors = tmp_path / "mirrors"
    mirror = ensure_commit(mirrors, str(repos["main"]), head)
    dest = tmp_path / "src"
    paths = checkout(mirrors, mirror, head, dest)
    assert paths == ["deps/dep", "deps/dep/third/leaf"]
    assert (dest / "deps/dep/third/leaf/leaf.txt").read_text() == "leaf"
    assert not (dest / "deps/dep/.git").exists()
