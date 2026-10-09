"""Replay a logged advisor run up to one consult, then let the live executor go on from there
with a different answer to that consult: Kimi's advice as logged, or a neutral reply. The
difference in outcome between the two branches is what that consult was worth.

pi itself rebuilds the run's state: a mock model server (`mock_server.py`) answers with the
logged assistant messages, so pi re-executes every tool call (the workspace and the session
come out as they were), and `agents/pi/replay.ts` stands in for the advisor plugin, answering
consults and refusing calls from the log. After the fork consult the mock server proxies to the
executor's endpoint, and later consults get the neutral reply: the executor is on its own, in
both branches.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from llm_second_opinion.contracts import Strict

# What the plugin answers when Kimi cannot be reached: the neutral branch looks like an outage.
NEUTRAL = "The advisor could not be reached. Continue on your own."
# Tool results of calls the plugin's gates refused (advisor-core session.ts REFUSED).
REFUSED = re.compile(r"^This (edit was not applied|call was not run)")
EXTENSION = Path(__file__).resolve().parents[3] / "agents/pi/replay.ts"


class ReplayOptions(Strict):
    """A pi agent option: replay `arm`'s run of the same task and seed in `experiment`."""

    experiment: str
    arm: str
    # The consult to fork at: its number in the run (1 = first), "last", or the first one of a
    # trigger (consult_value.TRIGGERS: report_gate, experiment, closing, come_back, stuck, own).
    consult: int | str
    branch: Literal["advice", "neutral"]


class NoFork(LookupError):
    """The source run has no such consult (or no run at all)."""


def source_dir(runs_dir: Path, opts: ReplayOptions, task: str, seed: int) -> Path:
    """The source item's counted attempt: the latest done row for that arm, task and seed."""
    from llm_second_opinion.ledger import Ledger
    from llm_second_opinion.scorers import attempt_rel

    exp_dir = runs_dir / opts.experiment
    rows = [r for r in Ledger(exp_dir / "ledger.sqlite").rows(opts.experiment)
            if (r["arm"], r["task"], r["seed"], r["status"]) == (opts.arm, task, seed, "done")]  # fmt: skip
    if not rows:
        raise NoFork(f"no done run of {opts.arm}/{task}/seed-{seed} in {opts.experiment}")
    return exp_dir / attempt_rel(max(rows, key=lambda r: r["updated"] or 0))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(b.get("text") or "") for b in content or [] if b.get("type") == "text")


def recording(message: dict) -> dict[str, Any]:
    """A logged assistant message as a mock-server completion."""
    blocks = message.get("content") or []
    calls = [
        {"id": b["id"], "type": "function",
         "function": {"name": b["name"], "arguments": json.dumps(b.get("arguments") or {})}}
        for b in blocks if b.get("type") == "toolCall"
    ]  # fmt: skip
    out: dict[str, Any] = {"content": _text(blocks) or None}
    thinking = "".join(str(b.get("thinking") or "") for b in blocks if b.get("type") == "thinking")
    if thinking:
        out["reasoning_content"] = thinking
    if calls:
        out["tool_calls"] = calls
    usage = message.get("usage") or {}
    return {
        "message": out,
        "finish_reason": message.get("rawStopReason") or ("tool_calls" if calls else "stop"),
        "usage": {
            "prompt_tokens": usage.get("input", 0),
            "completion_tokens": usage.get("output", 0),
            "total_tokens": usage.get("input", 0) + usage.get("output", 0),
        },
    }


def _addendum(section: str) -> str:
    """The appended system prompt as given, without the tags pi wraps it in."""
    m = re.fullmatch(r"\s*<addendum>\n?(.*?)\n?</addendum>\s*", section, re.DOTALL)
    return m.group(1) if m else section


def fork_number(rows: list[dict], consult: int | str) -> int:
    """The fork consult's number (1-based) from `consult_rows`."""
    if isinstance(consult, int):
        if not 1 <= consult <= len(rows):
            raise NoFork(f"the run has {len(rows)} answered consult(s), not {consult}")
        return consult
    if consult == "last" and rows:
        return len(rows)
    for r in rows:
        if r["trigger"] == consult:
            return r["consult"]
    raise NoFork(f"no {consult} consult in the run")


def build(src: Path, consult: int | str, branch: str) -> tuple[list[dict], dict[str, Any]]:
    """The mock server's recordings (every assistant message up to and including the one that
    calls the fork consult) and the script for replay.ts."""
    # Imported here: both import the pi adapter, which imports this module.
    from llm_second_opinion.consult_value import consult_rows
    from llm_second_opinion.scorers import jsonl

    pi = jsonl(src / "pi.jsonl")
    k = fork_number(consult_rows(jsonl(src / "events.jsonl"), pi), consult)
    messages = [e["message"] for e in pi if e.get("type") == "message_end"]
    results = {m["toolCallId"]: _text(m.get("content")) for m in messages
               if m.get("role") == "toolResult"}  # fmt: skip
    system = next((m for m in messages if m.get("role") == "system"), {})
    tool = next((t for t in system.get("toolsAdded") or [] if t.get("name") == "consult"), None)
    if tool is None:
        raise NoFork(f"{src}: the run has no consult tool")

    recordings: list[dict] = []
    answers: dict[str, str] = {}  # consult call id -> the tool result to give
    refused: dict[str, str] = {}  # other call id -> the refusal to give
    seen = 0
    fork_id = None
    for m in messages:
        if m.get("role") != "assistant" or m.get("stopReason") in ("error", "aborted"):
            continue
        recordings.append(recording(m))
        for b in m.get("content") or []:
            if b.get("type") != "toolCall":
                continue
            text = results.get(b["id"], "")
            if b["name"] == "consult":
                seen += 1
                if seen == k:
                    fork_id = b["id"]
                    answers[b["id"]] = text if branch == "advice" else NEUTRAL
                else:
                    answers[b["id"]] = text
            elif REFUSED.match(text):
                refused[b["id"]] = text
        if fork_id:
            break
    if fork_id is None:
        raise NoFork(f"{src}: consult {k} not found in pi.jsonl")

    # Messages the plugin appended at a turn's end (closing report, come back), by turn number,
    # up to the fork: a turn_end count, as replay.ts keeps it.
    appended: dict[int, list[str]] = {}
    turns = 0
    for e in pi:
        if e.get("type") == "turn_end":
            turns += 1
        elif e.get("type") == "entry_appended":
            entry = e.get("entry") or {}
            if entry.get("customType") == "lso-advice":
                appended.setdefault(turns + 1, []).append(str(entry.get("content") or ""))
        elif e.get("type") == "tool_execution_start" and e.get("toolCallId") == fork_id:
            break
    script = {
        "fork": fork_id,
        "fork_consult": k,
        "branch": branch,
        "answers": answers,
        "refused": refused,
        "appended": {str(t): v for t, v in appended.items()},
        "later": NEUTRAL,
        "guidance": _addendum((system.get("sections") or {}).get("addendum") or ""),
        "tool": {"description": tool.get("description"), "parameters": tool.get("parameters")},
    }
    return recordings, script


def fidelity(src: Path, replayed: Path) -> dict[str, Any]:
    """How faithfully a replay rebuilt its source up to the fork: the tool results of every
    call before it, compared by call id with digits masked (equal, or the first difference)."""
    from llm_second_opinion.scorers import jsonl

    script = json.loads((replayed / "replay.json").read_text())

    def results(d: Path) -> list[tuple[str, str]]:
        out = []
        for e in jsonl(d / "pi.jsonl"):
            m = e.get("message") if e.get("type") == "message_end" else None
            if isinstance(m, dict) and m.get("role") == "toolResult":
                out.append((m["toolCallId"], _text(m.get("content"))))
                if m["toolCallId"] == script["fork"]:
                    break
        return out

    def masked(r: tuple[str, str]) -> tuple[str, str]:
        return r[0], re.sub(r"\d+", "#", r[1])  # pids, timings, temp names

    a, b = results(src), results(replayed)
    pairs = list(zip(a, b, strict=False))
    same = sum(masked(x) == masked(y) for x, y in pairs)
    first = next(((x[0], x[1][:200], y[1][:200]) for x, y in pairs if masked(x) != masked(y)), None)
    return {
        "calls": len(a),
        "replayed": len(b),
        "equal_but_digits": same,
        "first_difference": first,
    }
