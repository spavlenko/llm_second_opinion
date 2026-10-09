"""Which consults matter: per consult, what made the executor ask (a gate, a prompt, or its own
choice), what Kimi said, whether the executor changed course, and how the run ended. Offline,
over stored runs; joins `bench uptake`'s verdicts when they exist. Rows go to
runs/<experiment>/consult-value.jsonl, one per consult. Correlational: a consult that changed
nothing in a resolved run may still have been what resolved it (replay answers that).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm_second_opinion.adapters.pi import _SHELL_EDIT, EDIT_TOOLS
from llm_second_opinion.config import Experiment
from llm_second_opinion.scorers import attempt_rel, jsonl
from llm_second_opinion.uptake import UPTAKE_FILE

VALUE_FILE = "consult-value.jsonl"
# What the executor was shown before a consult, by the plugin's texts (advisor-core session.ts).
TRIGGERS = (
    ("report_gate", "before your first change, file your investigation report"),
    ("report_gate", "You are stopping without having reported"),
    ("experiment", "first run the experiment you chose"),
    ("experiment", "report what your experiment showed"),
    ("closing", "Before you finish, file your closing report"),
    ("come_back", "turns without a report to the advisor"),
    ("stuck", "tool calls failed (the latest"),
)
# A closing review's verdict: the case-review prompt puts the rejection reason first, so the
# whole answer is searched; "Almost" and "one thing missing" read as not done.
NOT_DONE = re.compile(r"\bnot done\b|\balmost\b|\bone thing missing\b", re.IGNORECASE)
DONE = re.compile(r"\bDone\b")


def verdict(advice: str) -> str | None:
    if NOT_DONE.search(advice):
        return "not done"
    return "done" if DONE.search(advice) else None


def _shown(e: dict) -> str:
    """Text the plugin put in front of the executor: a tool result or an appended message."""
    if e.get("type") == "entry_appended":
        return str((e.get("entry") or {}).get("content") or "")
    m = e.get("message") if e.get("type") == "message_end" else None
    if isinstance(m, dict) and m.get("role") == "toolResult":
        c = m.get("content")
        return c if isinstance(c, str) else " ".join(str(b.get("text") or "") for b in c or [])
    return ""


def _edited(e: dict) -> str | None:
    """The file an edit call changes (`*` for a shell edit), None for any other call."""
    args = e.get("args") if isinstance(e.get("args"), dict) else {}
    if e.get("toolName") in EDIT_TOOLS:
        return str(args.get("path") or args.get("file_path") or "?")
    if e.get("toolName") == "bash" and _SHELL_EDIT.search(str(args.get("command") or "")):
        return "*"
    return None


def consult_rows(events: list[dict], pi_events: list[dict]) -> list[dict[str, Any]]:
    """Per answered consult, in order: trigger, turn, Kimi tokens, closing verdict, and the
    executor's edits before and after it (until the next consult or the end)."""
    calls = [i for i, e in enumerate(pi_events)
             if e.get("type") == "tool_execution_start" and e.get("toolName") == "consult"]  # fmt: skip
    asked = [e for e in events if e.get("type") == "consult_requested"]
    requests = [e for e in events if e.get("type") == "advisor_request"]
    responses = {e.get("request_id"): e for e in events if e.get("type") == "advisor_response"}
    rows = []
    for k, req in enumerate(requests):
        rid = req.get("request_id")
        resp = responses.get(rid)
        if resp is None:
            continue
        at = calls[k] if k < len(calls) else len(pi_events)
        prev = calls[k - 1] if k else 0
        nxt = calls[k + 1] if k + 1 < len(calls) else len(pi_events)
        shown = " ".join(_shown(e) for e in pi_events[prev:at])
        trigger = next((name for name, text in TRIGGERS if text in shown), "own")
        before = {f for e in pi_events[:at] if (f := _edited(e))}
        after = [f for e in pi_events[at + 1 : nxt] if (f := _edited(e))]
        rows.append(
            {
                "consult": k + 1,
                "request_id": rid,
                "trigger": trigger,
                "turn": asked[k].get("turn") if k < len(asked) else None,
                "kimi_in": int(resp.get("prompt_tokens") or req.get("input_tokens") or 0),
                "kimi_out": int(resp.get("output_tokens") or 0),
                "verdict": verdict(str(resp.get("advice_text") or ""))
                if trigger == "closing"
                else None,
                "edits_after": len(after),
                "new_files_after": len({f for f in after if f != "*"} - before),
            }
        )
    return rows


def value_experiment(
    exp: Experiment,
    runs_dir: Path,
    *,
    arm: str | None = None,
    echo: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Rows for every done item at its arm's current config hash, written to
    runs/<experiment>/consult-value.jsonl, and a summary per arm and trigger."""
    from llm_second_opinion.ledger import Ledger
    from llm_second_opinion.report import current_rows, recorded_hashes
    from llm_second_opinion.runner import task_image
    from llm_second_opinion.tasks import Manifest

    exp_dir = runs_dir / exp.name
    ledger = Ledger(exp_dir / "ledger.sqlite")
    tasks = {t.id: t for t in exp.select(Manifest.from_yaml(exp.tasks)).tasks}
    hashes = recorded_hashes(exp, ledger.fingerprints(exp.name))
    images = {t.id: task_image(t) for t in tasks.values()}
    judged = {(r["attempt"], r["request_id"]): r for r in jsonl(exp_dir / UPTAKE_FILE)}
    out: list[dict[str, Any]] = []
    for row in current_rows(exp, ledger.rows(exp.name), hashes, images):
        if row["status"] != "done" or row["task"] not in tasks or arm not in (None, row["arm"]):
            continue
        rel = attempt_rel(row)
        d = exp_dir / rel
        grade = json.loads((d / "grade.json").read_text()) if (d / "grade.json").exists() else {}
        resolved = bool(grade.get("resolved", row.get("resolved")))
        for c in consult_rows(jsonl(d / "events.jsonl"), jsonl(d / "pi.jsonl")):
            j = judged.get((rel, c["request_id"]), {})
            out.append({"arm": row["arm"], "task": row["task"], "seed": row["seed"],
                        "attempt": rel, "resolved": resolved, **c,
                        "right": j.get("right"), "followed": j.get("followed")})  # fmt: skip
    (exp_dir / VALUE_FILE).write_text("".join(json.dumps(r) + "\n" for r in out))
    for line in summary(out):
        echo(line)
    echo(f"wrote {len(out)} consult row(s) to {exp_dir / VALUE_FILE}")
    return out


def _pct(n: int, d: int) -> str:
    return f"{n}/{d}" if d else "-"


def summary(rows: list[dict[str, Any]]) -> list[str]:
    """Per arm and trigger: consults, Kimi tokens each, how often the executor edited after
    it, the judge's right and followed (when judged), and resolved runs; closing reviews by
    verdict and outcome."""
    lines = []
    for name in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == name]
        lines.append(f"{name}: {len(rs)} consults")
        for t in sorted({r["trigger"] for r in rs}):
            ts = [r for r in rs if r["trigger"] == t]
            judged = [r for r in ts if r["right"]]
            tokens = sum(r["kimi_in"] + r["kimi_out"] for r in ts) / len(ts)
            lines.append(
                f"  {t:12} n={len(ts):3}  kimi {tokens / 1000:4.1f}k each  "
                f"edits after {_pct(sum(r['edits_after'] > 0 for r in ts), len(ts))}  "
                f"right {_pct(sum(r['right'] == 'yes' for r in judged), len(judged))}  "
                f"right+followed {_pct(sum(r['right'] == r['followed'] == 'yes' for r in judged), len(judged))}  "
                f"run resolved {_pct(sum(r['resolved'] for r in ts), len(ts))}"
            )
        closing = [r for r in rs if r["trigger"] == "closing"]
        if closing:
            cells = [
                f"{v} & {'resolved' if res else 'failed'} {sum(r['verdict'] == v and r['resolved'] == res for r in closing)}"
                for v in ("done", "not done")
                for res in (True, False)
            ]
            lines.append("  closing: " + ", ".join(cells))
    return lines
