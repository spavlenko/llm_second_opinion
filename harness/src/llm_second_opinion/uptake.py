"""Advice uptake, per consult: was the advice right (against the maintainers' fix), and did the
final patch follow it? A judge model reads the issue, the upstream fix, each consult's report
and advice, and the final patch: one call per attempt, cached by its text (`Prober`'s cache,
usage role `probe`, each attempt's calls in `judge-usage.jsonl`). Rows go to
runs/<experiment>/uptake.jsonl, one per consult. Offline: nothing here runs during a run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm_second_opinion.config import ConfigError, Experiment
from llm_second_opinion.scorers import Prober, attempt_rel

UPTAKE_FILE = "uptake.jsonl"
GRADES = ("yes", "partly", "no", "na")
# Character cuts per part of the judge's prompt; the gold fix gets the most.
GOLD_CHARS = 12000
PATCH_CHARS = 8000
REPORT_CHARS = 2500
ADVICE_CHARS = 3000

JUDGE_SYSTEM = (
    "You grade the advice a developer got while fixing a bug, against the fix the "
    "maintainers made. Be strict and brief."
)
JUDGE_PROMPT = """{text}

For each consult, exactly one line:
C<k>: right=<yes|partly|no|na> followed=<yes|partly|no|na> | <reason, at most 20 words>

right: does the advice's leading diagnosis (or, for a closing review, its verdict) point to \
the maintainers' fix: the same place and the same behaviour? partly: the right place or \
behaviour, not both. na: the advice makes no claim about the cause or the fix.
followed: does the final patch do what that advice said? na when right is na."""

LINE = re.compile(
    r"^\W*C(\d+)\W*:?\s*right\s*=\s*(\w+)\s+followed\s*=\s*(\w+)\s*\|?\s*(.*)$", re.IGNORECASE
)


class Judge(Prober):
    VERSION = "uptake-1"
    SYSTEM = JUDGE_SYSTEM
    PROMPT = JUDGE_PROMPT
    USAGE = "judge-usage.jsonl"

    def parse(self, answer: str) -> dict[int, dict[str, str]]:
        return parse_verdicts(answer)


def parse_verdicts(answer: str) -> dict[int, dict[str, str]]:
    """`C<k>: right=.. followed=.. | reason` lines, by k; unknown grades read as `na`."""
    out: dict[int, dict[str, str]] = {}
    for line in answer.splitlines():
        m = LINE.match(line.replace("*", "").replace("`", "").strip())
        if not m:
            continue
        right, followed = (g.lower() if g.lower() in GRADES else "na" for g in m.group(2, 3))
        out[int(m.group(1))] = {"right": right, "followed": followed, "reason": m.group(4).strip()}
    return out


def consults(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Each answered consult in order: the report part of its brief and the advice (the last
    response to its request, after any clarify follow-up)."""
    briefs: dict[str, str] = {}
    answers: dict[str, str] = {}
    for e in events:
        if e.get("type") == "advisor_request":
            briefs[e["request_id"]] = e.get("brief_text") or ""
        elif e.get("type") == "advisor_response" and e.get("advice_text"):
            answers[e["request_id"]] = e["advice_text"]
    out = []
    for rid, brief in briefs.items():
        if rid in answers:
            at = brief.find("Their report:")
            out.append(
                {
                    "request_id": rid,
                    "report": brief[at:] if at >= 0 else brief,
                    "advice": answers[rid],
                }
            )
    return out


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else f"{text[:n]}\n[cut: {len(text) - n} more characters]"


def judge_text(
    issue: str, gold: str, items: list[dict[str, str]], patch: str, resolved: bool
) -> str:
    parts = [
        f"The issue:\n<issue>\n{issue.strip()}\n</issue>",
        f"The maintainers' fix (tests written for it are hidden from the developer):\n```diff\n{_cut(gold, GOLD_CHARS)}\n```",
        f"The developer consulted an advisor {len(items)} time(s). Each consult is the developer's report, then the advice.",
    ]
    for k, c in enumerate(items, 1):
        parts.append(
            f"--- C{k}: report\n{_cut(c['report'].strip(), REPORT_CHARS)}\n"
            f"--- C{k}: advice\n{_cut(c['advice'].strip(), ADVICE_CHARS)}"
        )
    verdict = "resolved" if resolved else "not resolved"
    parts.append(
        f"The developer's final patch ({verdict} by the hidden tests):\n```diff\n{_cut(patch, PATCH_CHARS) or '(empty)'}\n```"
    )
    return "\n\n".join(parts)


def judge_experiment(
    exp: Experiment,
    runs_dir: Path,
    judge: Judge,
    *,
    arm: str | None = None,
    task: str | None = None,
    echo: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Judge every consult of the done items at their arm's current config hash (as `bench
    report` counts them) and merge them into runs/<experiment>/uptake.jsonl."""
    from llm_second_opinion.ledger import Ledger
    from llm_second_opinion.report import current_rows, recorded_hashes
    from llm_second_opinion.runner import task_image
    from llm_second_opinion.tasks import Manifest

    exp_dir = runs_dir / exp.name
    ledger = Ledger(exp_dir / "ledger.sqlite")
    tasks = {t.id: t for t in exp.select(Manifest.from_yaml(exp.tasks)).tasks}
    if task is not None and task not in tasks:
        raise ConfigError(f"no task {task!r} in {exp.tasks}")
    if arm is not None and arm not in {a.name for a in exp.arms}:
        raise ConfigError(f"no arm named {arm!r}")
    hashes = recorded_hashes(exp, ledger.fingerprints(exp.name))
    images = {t.id: task_image(t) for t in tasks.values()}
    rows = [
        r
        for r in current_rows(exp, ledger.rows(exp.name), hashes, images)
        if r["status"] == "done" and r["task"] in tasks
        and arm in (None, r["arm"]) and task in (None, r["task"])
    ]  # fmt: skip
    out: list[dict[str, Any]] = []
    for row in rows:
        rel = attempt_rel(row)
        d = exp_dir / rel
        events = (
            [
                json.loads(line)
                for line in (d / "events.jsonl").read_text().splitlines()
                if line.strip()
            ]
            if (d / "events.jsonl").exists()
            else []
        )
        items = consults(events)
        if not items:
            continue
        t = tasks[row["task"]]
        if not t.gold_patch:
            raise ConfigError(f"{t.id} has no gold_patch in {exp.tasks}")
        grade = json.loads((d / "grade.json").read_text()) if (d / "grade.json").exists() else {}
        resolved = bool(grade.get("resolved", row.get("resolved")))
        patch = (d / "patch.diff").read_text() if (d / "patch.diff").exists() else ""
        with judge.attempt(d) as ask:
            verdicts = ask(judge_text(t.problem_statement, t.gold_patch, items, patch, resolved))
        for k, c in enumerate(items, 1):
            v = verdicts.get(k, {"right": "na", "followed": "na", "reason": "(no verdict)"})
            out.append({"arm": row["arm"], "task": row["task"], "seed": row["seed"], "attempt": rel,
                        "consult": k, "request_id": c["request_id"], "resolved": resolved, **v})  # fmt: skip
    # Merged: rows of items not judged this time (another arm or task) are kept.
    path = exp_dir / UPTAKE_FILE
    judged = {(r["arm"], r["task"], r["seed"]) for r in rows}
    old = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    kept = [r for r in old if (r["arm"], r["task"], r["seed"]) not in judged]
    path.write_text("".join(json.dumps(r) + "\n" for r in kept + out))
    for name, s in summary(out).items():
        echo(f"{name}: {s}")
    echo(f"wrote {len(out)} consult verdict(s) to {path}")
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Per arm: consults judged, how many were right, and how many right ones were followed."""
    out = {}
    for name in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == name]
        right = [r for r in rs if r["right"] == "yes"]
        partly = sum(r["right"] == "partly" for r in rs)
        wrong = sum(r["right"] == "no" for r in rs)
        followed = sum(r["followed"] == "yes" for r in right)
        out[name] = (
            f"{len(rs)} consults; right {len(right)}, partly {partly}, wrong {wrong}; "
            f"right and followed {followed}/{len(right)}"
        )
    return out
