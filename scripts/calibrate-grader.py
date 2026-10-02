"""Grader calibration: negative controls on real tasks. The grader must fail an empty patch and
a no-op patch (a comment added to a file the gold patch modifies). Positive control: run
experiments/mswe-smoke-gold.yaml (gold must resolve).

    cd harness && .venv/bin/python ../scripts/calibrate-grader.py [TASK_ID ...]
"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_second_opinion.grading import grade
from llm_second_opinion.runtime import Runtime
from llm_second_opinion.tasks import Manifest

rt = Runtime()
only = sys.argv[1:]
tasks = Manifest.from_yaml(
    Path(__file__).resolve().parents[1] / "tasks/manifests/mswe-mini-cpp-v2-smoke.yaml"
).tasks
tasks = [t for t in tasks if not only or t.id in only]


def noop_patch(task):
    # a file the gold patch modifies and that exists at the base commit (not a new file)
    path = re.search(r"^--- a/(\S+)", task.gold_patch, re.MULTILINE).group(1)
    box = rt.start(task.image, 2, 2, f"calib noop {task.id}")
    try:
        box.exec(f"sed -i '1i // calibration no-op' {path}", workdir=task.workdir)
        return box.exec("git diff", workdir=task.workdir).output
    finally:
        box.remove()


def run(task):
    out = []
    for label, patch in (("empty", ""), ("noop", noop_patch(task))):
        box = rt.start(task.image, 4, 4, f"calib {label} {task.id}")
        try:
            g = grade(box, task, patch, 1200)
        finally:
            box.remove()
        f2p = [t for t in task.tests.fail_to_pass if g.tests.get(t) == "passed"]
        out.append(
            f"{task.id:28} {label:6} resolved={g.resolved} reason={g.reason} f2p_passing={len(f2p)}/{len(task.tests.fail_to_pass)}"
        )
    return out


with ThreadPoolExecutor(3) as pool:
    for lines in pool.map(run, tasks):
        print("\n".join(lines), flush=True)
