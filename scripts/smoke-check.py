"""Flag anomalies in a smoke run: one line per problem, nothing when the run is clean, then
the anti-delegation counts: consults refused (by rule), advice code lines cut, briefs cut.

    harness/.venv/bin/python scripts/smoke-check.py RUNS_DIR/<experiment>

Walks every item directory (any directory holding result.json) and checks artifacts, events,
advisor exchanges, leakage below L3, briefs cut at max_brief_tokens, and proxy usage against
plugin events.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness/src"))
from llm_second_opinion.contracts import parse_events
from llm_second_opinion.scorers import role_leaks

AGENT_FILES = ("pi.jsonl", "timeline.jsonl", "usage.jsonl", "patch.diff", "grade.log")


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def brief_leaks(events: list[dict], level: str) -> list[str]:
    """Below L3: one problem per text sent to the advisor (a brief, or a `clarify` follow-up)
    that holds a role-map name raw, by the scorers' rule (`role_leaks`: projects in any case,
    files by path or file name, member accesses count; short plain lowercase words do not)."""
    if level == "L3":
        return []
    names: dict[str, str] = {}
    problems = []
    for e in events:
        if e["type"] == "brief_built":
            names |= e.get("role_map") or {}
        elif e["type"] in ("advisor_request", "advisor_followup"):
            text = e["brief_text"] if e["type"] == "advisor_request" else e["sent_text"]
            leaked = sorted({n for p, n in names.items() if role_leaks(p, n, text)})
            if leaked:
                what = "brief" if e["type"] == "advisor_request" else "follow-up"
                problems.append(f"{e['request_id']}: {level} {what} contains {leaked}")
    return problems


def counts(item: Path) -> Counter[str]:
    """Consults refused (`refused` and `refused:<rule>`), advice code lines cut, briefs cut."""
    out: Counter[str] = Counter()
    for e in jsonl(item / "events.jsonl"):
        if e.get("type") == "consult_refused":
            out["refused"] += 1
            out[f"refused:{e.get('reason')}"] += 1
        elif e.get("type") == "advice_applied":
            out["code_lines_removed"] += e.get("code_lines_removed") or 0
        elif e.get("type") == "brief_built" and e.get("truncated"):
            out["briefs_truncated"] += 1
    return out


def describe(c: Counter[str]) -> str:
    rules = ", ".join(
        f"{k.split(':', 1)[1]} {n}" for k, n in sorted(c.items()) if k.startswith("refused:")
    )
    return (
        f"consults refused {c['refused']}{f' ({rules})' if rules else ''}; "
        f"advice code lines cut {c['code_lines_removed']}; briefs cut {c['briefs_truncated']}"
    )


def check(item: Path) -> list[str]:
    problems: list[str] = []
    result = json.loads((item / "result.json").read_text())
    config = json.loads((item / "advisor.json").read_text())
    advisor = config.get("advisor")

    for name in AGENT_FILES:
        if not (item / name).exists():
            problems.append(f"missing {name}")
    if result["exit_reason"] == "crash":
        problems.append(f"crash: {(result.get('detail') or '')[:200]}")
    elif result["exit_reason"] != "finished":
        problems.append(f"exit {result['exit_reason']} after {result['turns']} turns")
    stderr = (item / "pi.stderr").read_text().strip() if (item / "pi.stderr").exists() else ""
    if stderr:
        problems.append(f"pi stderr: {stderr[-200:]}")

    usage = jsonl(item / "usage.jsonl")
    for u in usage:
        if u["status"] != 200:
            problems.append(f"usage: {u['role']} call {u['seq']} status {u['status']}")

    if not advisor:
        return problems

    text = (item / "events.jsonl").read_text() if (item / "events.jsonl").exists() else ""
    try:
        events = [e.model_dump() for e in parse_events(text)]
    except Exception as e:  # noqa: BLE001 - report any schema failure
        return problems + [f"events.jsonl fails the schema: {str(e)[:300]}"]
    if not events:
        return problems + ["advisor arm with no events"]
    if events[0]["type"] != "policy_rendered":
        problems.append(f"first event is {events[0]['type']}, not policy_rendered")

    level = advisor["level"]
    problems += brief_leaks(events, level)
    for e in events:
        kind = e["type"]
        if kind == "brief_built":
            if e["truncated"]:
                problems.append(f"brief cut at max_brief_tokens ({e['tokens']} tokens)")
        elif kind == "advisor_request":
            brief = e["brief_text"]
            if len(brief) < 120:
                problems.append(f"{e['request_id']}: brief only {len(brief)} chars")
        elif kind == "advisor_response":
            if e.get("finish_reason") == "length":
                problems.append(
                    f"{e['request_id']}: advice truncated ({e['output_tokens']} tokens)"
                )
            if not e["advice_text"].strip():
                problems.append(f"{e['request_id']}: empty advice")
        elif kind == "advisor_error":
            problems.append(
                f"{e['request_id']}: advisor error {e.get('status')}: {e['message'][:150]}"
            )
        elif kind == "budget_exhausted":
            problems.append(f"budget exhausted ({e['consults_used']}/{e['limit']})")

    requests = sum(e["type"] == "advisor_request" for e in events)
    metered = sum(u["role"] == "advisor" for u in usage)
    if requests != metered:
        problems.append(f"{requests} advisor_request events but {metered} metered advisor calls")
    applied = sum(e["type"] == "advice_applied" for e in events)
    answered = sum(e["type"] == "advisor_response" for e in events)
    if applied != answered:
        problems.append(f"{answered} advisor responses but {applied} applied")
    return problems


def main(root: Path) -> int:
    items = sorted(p.parent for p in root.rglob("result.json"))
    if not items:
        print(f"no items under {root}")
        return 1
    bad = 0
    total: Counter[str] = Counter()
    for item in items:
        problems = check(item)
        c = counts(item)
        total += c
        if problems or c["refused"] or c["code_lines_removed"]:
            print(f"{item.relative_to(root)}")
        for p in problems:
            print(f"  - {p}")
        if c["refused"] or c["code_lines_removed"]:
            print(f"  · {describe(c)}")
        bad += bool(problems)
    print(f"{len(items)} item(s), {bad} with problems")
    print(describe(total))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
