"""Offline uncertainty analysis: which local signals, visible to the executor's own machine,
predict that a local-only run fails? One row per graded A0 run, then each signal's AUC for
failure (bootstrap over tasks, since seeds of one task are not independent).

    harness/.venv/bin/python scripts/uncertainty-signals.py RUNS_DIR/<experiment> ... \
        [--csv OUT.csv]

Signals are end-of-run values; a gate inside a run would see a prefix of them. Nothing here
reads the hidden tests: the label comes from grade.json, the signals from pi.jsonl, metrics.json
and patch.diff.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

CTEST = re.compile(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)")
# runs the test script (reading it with cat/head/sed does not count)
RUNS_TESTS = re.compile(r"(?:^|[;&|(]\s*|\s)(?:/opt/lso/)?run-tests(?=\s|$|[;&|)])")
READS_SCRIPT = re.compile(r"\b(?:cat|head|tail|less|sed|grep|ls|wc)\s[^|;&]*run-tests")


# writes a C/C++ source outside the repository's tests and compiles it: a reproduction
WRITES_SOURCE = re.compile(r"(?:cat\s*>|tee)\s*\S+\.(?:cpp|cc|cxx|c)\b")
COMPILES = re.compile(r"\b(?:g\+\+|gcc|c\+\+|clang\+\+)\b")


def runs_tests(command: str) -> bool:
    return bool(RUNS_TESTS.search(command)) and not READS_SCRIPT.search(command)


def tool_calls(pi_log: Path) -> list[dict]:
    """(name, args, output, exit_code) per tool call, in order."""
    starts, calls = {}, []
    for line in pi_log.open():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "tool_execution_start":
            starts[r["toolCallId"]] = r
        elif r.get("type") == "tool_execution_end":
            s = starts.get(r["toolCallId"], {})
            res = r.get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
            code = (res.get("structuredContent") or {}).get("exit_code")
            calls.append(
                {
                    "name": r.get("toolName"),
                    "args": s.get("args") or {},
                    "out": text,
                    "code": code,
                    "error": bool(r.get("isError")),
                }
            )
    return calls


def signals(d: Path) -> dict:
    m = json.loads((d / "metrics.json").read_text())
    res = json.loads((d / "result.json").read_text())
    calls = tool_calls(d / "pi.jsonl")
    tests = [c for c in calls if c["name"] == "bash" and runs_tests(c["args"].get("command", ""))]
    last = tests[-1] if tests else None
    edits = [c for c in calls if c["name"] in ("edit", "write")]
    edits_per_file = defaultdict(int)
    for c in edits:
        edits_per_file[c["args"].get("path", "")] += 1
    failed = [c for c in calls if c["error"] or (c["code"] not in (None, 0))]
    errors = [c["out"].strip().splitlines()[-1][:120] for c in failed if c["out"].strip()]
    tail = calls[-10:]
    bash = [c["args"].get("command", "") for c in calls if c["name"] == "bash"]
    wrote = any(WRITES_SOURCE.search(b) for b in bash) or any(
        c["name"] == "write" and re.search(r"\.(?:cpp|cc|cxx|c)$", c["args"].get("path", ""))
        for c in calls
    )
    compiled = sum(1 for b in bash if COMPILES.search(b))
    diff = (d / "patch.diff").read_text() if (d / "patch.diff").exists() else ""
    plus = sum(1 for x in diff.splitlines() if x.startswith("+") and not x.startswith("+++"))
    minus = sum(1 for x in diff.splitlines() if x.startswith("-") and not x.startswith("---"))
    files = re.findall(r"^diff --git a/(\S+)", diff, re.MULTILINE)
    usage = next((u for u in res.get("usage", []) if u.get("role") == "executor"), {})
    ct = CTEST.search(last["out"]) if last else None
    # the executor's own verdict: it pipes run-tests into `tail`, so the exit code is lost and the
    # build-failed notice may be cut; ctest's summary line survives. Pass = summary with 0 failed.
    verdicts = [(s := CTEST.search(c["out"])) is not None and s[2] == "0" for c in tests]
    return {
        # the executor's own verdict
        "no_test_run": int(not tests),
        "last_test_failed": int(bool(tests) and not verdicts[-1]),
        "ever_tests_passed": int(any(verdicts)),
        "last_build_failed": int(bool(last) and "build failed" in last["out"]),
        "last_ctest_failed": int(ct[2]) if ct else -1,
        "test_runs": len(tests),
        "test_runs_failed": verdicts.count(False),
        "no_repro": int(not (wrote and compiled)),
        "own_compiles": compiled,
        # trajectory
        "turns": m.get("turns") or 0,
        "hit_limit": int(res.get("exit_reason") != "finished"),
        "first_edit_turn": m.get("first_edit_turn") or -1,
        "edits": len(edits),
        "max_edits_one_file": max(edits_per_file.values(), default=0),
        "tool_errors": len(failed),
        "tool_error_rate_last10": sum(1 for c in tail if c in failed) / max(len(tail), 1),
        "repeated_errors": len(errors) - len(set(errors)),
        "nudges": m.get("tool_call_nudges") or 0,
        "max_context_ktok": round((m.get("max_context_tokens") or 0) / 1000, 1),
        "reasoning_ktok": round((usage.get("reasoning_tokens") or 0) / 1000, 1),
        # the patch
        "empty_patch": int(not diff.strip()),
        "patch_lines": plus + minus,
        "patch_files": len(files),
        "touches_header": int(any(f.endswith((".h", ".hpp")) for f in files)),
        "touches_tests": int(any("test" in f.lower() for f in files)),
    }


def runs(exp_dirs: list[Path]):
    """The graded attempt per (experiment, task, seed): the last attempt with grade.json."""
    for exp in exp_dirs:
        for grade in sorted((exp / "A0").glob("*/seed-*/*/attempt-*/grade.json")):
            d = grade.parent
            item = json.loads((d / "item.json").read_text())
            g = json.loads(grade.read_text())
            yield {
                "experiment": item["experiment"],
                "task": item["task"],
                "seed": item["seed"],
                "attempt": item["attempt"],
                "resolved": int(bool(g.get("resolved"))),
            } | signals(d)


def auc(scores: list[float], labels: list[int]) -> float | None:
    """P(score of a failed run > score of a resolved run); ties count half."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", nargs="+", type=Path)
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--boot", type=int, default=1000)
    args = ap.parse_args()
    rows = {}
    for r in runs(args.experiments):  # later attempts replace earlier ones
        rows[(r["experiment"], r["task"], r["seed"])] = r
    rows = list(rows.values())
    if not rows:
        sys.exit("no graded A0 runs found")
    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    failed = [1 - r["resolved"] for r in rows]
    tasks = sorted({r["task"] for r in rows})
    by_task = defaultdict(list)
    for i, r in enumerate(rows):
        by_task[r["task"]].append(i)
    print(
        f"{len(rows)} runs, {len(tasks)} tasks, {sum(failed)} failed ({sum(failed) / len(rows):.0%})"
    )
    print(f"\n{'signal (higher = more likely failed)':38} {'AUC':>5}  {'95% CI (tasks)':>14}")
    keys = [k for k in rows[0] if k not in ("experiment", "task", "seed", "attempt", "resolved")]
    rng = random.Random(0)
    out = []
    for k in keys:
        x = [r[k] for r in rows]
        a = auc(x, failed)
        boots = []
        for _ in range(args.boot):
            idx = [i for t in rng.choices(tasks, k=len(tasks)) for i in by_task[t]]
            b = auc([x[i] for i in idx], [failed[i] for i in idx])
            if b is not None:
                boots.append(b)
        boots.sort()
        lo, hi = boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots)) - 1]
        out.append((abs(a - 0.5), k, a, lo, hi))
    for _, k, a, lo, hi in sorted(out, reverse=True):
        print(f"{k:38} {a:5.2f}  {lo:5.2f}-{hi:4.2f}")
    # the executor's own verdict as a rule: fail if it never ran the tests or the last run failed
    pred = [r["no_test_run"] or r["last_test_failed"] or r["empty_patch"] for r in rows]
    for name, flag in (("no_test_run", "no_test_run"), ("last_test_failed", "last_test_failed")):
        on = [1 - r["resolved"] for r in rows if r[flag]]
        print(f"{name}: {len(on)} runs, of which failed {sum(on)}")
    tp = sum(p and y for p, y in zip(pred, failed))
    print(
        f"\nrule 'no test run, last run failed, or empty patch' -> failure: flags {sum(pred)}"
        f"/{len(rows)}, precision {tp / max(sum(pred), 1):.0%}, recall {tp / max(sum(failed), 1):.0%}"
    )
    quiet = [y for p, y in zip(pred, failed) if not p]
    print(f"runs the rule calls fine: {len(quiet)}, of which failed {sum(quiet)}")


if __name__ == "__main__":
    main()
