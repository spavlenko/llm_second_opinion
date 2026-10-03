"""Trajectory metrics per attempt (`metrics.json`): the agent's own (`AgentAdapter.metrics`,
from its logs) plus those every agent gets, from the advisor's events, the metered usage, and
the patch."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from llm_second_opinion.grading import diff_files
from llm_second_opinion.metering import read_usage


def extract(item_dir: Path, agent: dict[str, Any]) -> dict[str, Any]:
    """All metrics for one attempt directory; `agent` is the adapter's. Times are seconds
    since the agent started (`agent_start_ts` from the adapter, if it knows), turns count
    from 0."""
    agent = dict(agent)
    t0 = agent.pop("agent_start_ts", None)
    nudges = _records(item_dir / "nudges.jsonl")
    return (
        agent
        | context_metrics(item_dir)
        | advisor_metrics(item_dir, t0)
        | {"tool_call_nudges": len(nudges)}  # tool calls written as text (toolcall-nudge.ts)
    )


def context_metrics(item_dir: Path) -> dict[str, Any]:
    """Largest and last executor prompt (the context the executor sent), from the proxy."""
    prompts = [
        r.prompt_tokens
        for r in read_usage(item_dir / "usage.jsonl")
        if r.role == "executor" and 200 <= r.status < 300
    ]
    return {
        "max_context_tokens": max(prompts) if prompts else None,
        "final_context_tokens": prompts[-1] if prompts else None,
    }


def advisor_metrics(item_dir: Path, t0: float | None = None) -> dict[str, Any]:
    """Consults by trigger, the first one, advisor errors, and whether the final patch
    touches files the advice named (by path or file name)."""
    events = _records(item_dir / "events.jsonl")
    starts = [e for e in events if e.get("type") in ("consult_requested", "trigger_fired")]
    by_trigger = Counter(
        "consult" if e["type"] == "consult_requested" else str(e.get("intervention"))
        for e in starts
    )
    first = starts[0] if starts else None
    advice = [
        str(e.get("injected_text") or "") for e in events if e.get("type") == "advice_applied"
    ]
    touched = patch_files((item_dir / "patch.diff").read_text()) if advice else []
    named = sorted(f for f in touched if any(_names(f, text) for text in advice))
    return {
        "consults": len(starts),
        "consults_by_trigger": dict(sorted(by_trigger.items())),
        "consults_refused": sum(e.get("type") == "consult_refused" for e in events),
        "advisor_errors": sum(e.get("type") == "advisor_error" for e in events),
        "advice_applied": len(advice),
        "budget_exhausted": any(e.get("type") == "budget_exhausted" for e in events),
        "first_consult_turn": first.get("turn") if first else None,
        "first_consult_s": (
            round(first["ts"] - t0, 3) if first and t0 is not None and "ts" in first else None
        ),
        "patch_files": len(touched) if advice else None,
        "advised_files_touched": named if advice else None,
        "patch_touches_advised_files": bool(named) if advice else None,
    }


def patch_files(diff: str) -> list[str]:
    """Files a git diff changes (new paths)."""
    return sorted(diff_files(diff))


def _names(path: str, text: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path in text or re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text) is not None


def numeric(metrics: dict[str, Any]) -> dict[str, float]:
    """The metrics MLflow can log: numbers and booleans, nested counts flattened
    (`tool_calls_by_name.bash` -> `tool_calls_bash`, `consults_by_trigger.stuck` ->
    `consults_stuck`); None and lists are left out."""
    out: dict[str, float] = {}
    for name, value in metrics.items():
        if isinstance(value, dict):
            prefix = name.removesuffix("_by_name").removesuffix("_by_trigger")
            out |= {f"{prefix}_{k}": float(v) for k, v in value.items() if _is_number(v)}
        elif _is_number(value):
            out[name] = float(value)
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and value is not None


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records
