"""Spans for a work item's trace, built after the fact from what the agent recorded.

Adapters turn their agent's logs into `Span` trees (`AgentAdapter.spans`); the runner adds
the item and grading spans, and `tracking.Tracker` writes the tree to MLflow as one trace.
Kept free of MLflow imports, so adapters do not depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# MLflow span types used here: https://mlflow.org/docs/latest/genai/tracing/
Kind = Literal["AGENT", "CHAIN", "CHAT_MODEL", "TOOL", "EVALUATOR", "UNKNOWN"]

MAX_TEXT = 20_000  # characters kept per input or output value


@dataclass
class Span:
    name: str
    kind: Kind
    start_ns: int
    end_ns: int
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    children: list[Span] = field(default_factory=list)


def ms_to_ns(ms: float) -> int:
    return int(ms * 1_000_000)


def clip(value: Any) -> Any:
    """Long strings shortened, keeping both ends; nested values clipped recursively."""
    if isinstance(value, str) and len(value) > MAX_TEXT:
        half = MAX_TEXT // 2
        return (
            f"{value[:half]}\n[... {len(value) - MAX_TEXT} characters omitted ...]\n{value[-half:]}"
        )
    if isinstance(value, dict):
        return {k: clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clip(v) for v in value]
    return value


def s_to_ns(s: float) -> int:
    return int(s * 1_000_000_000)


def advisor_spans(events: list[dict], advice: dict[str, dict] | None = None) -> list[Span]:
    """One span per consult from the plugin's events.jsonl: trigger or request, then the brief
    and the advisor call, ending when the advice was applied. `advice` (request id -> record of
    the plugin's advice.jsonl) adds the system prompt and the advice text, which events do
    not carry. A consult refused (by a consult rule or the budget) is a span of its own."""
    advice = advice or {}
    groups: list[dict[str, dict]] = []
    by_request: dict[str, dict[str, dict]] = {}
    for e in events:
        kind = e.get("type")
        if kind in ("trigger_fired", "consult_requested", "budget_exhausted", "consult_refused"):
            groups.append({"start": e})
        elif kind in ("brief_built", "advisor_request") and groups:
            groups[-1][kind] = e
            if kind == "advisor_request":
                by_request[e["request_id"]] = groups[-1]
        elif kind in ("advisor_response", "advisor_error", "advice_applied") and (
            e.get("request_id") in by_request
        ):
            by_request[e["request_id"]][kind] = e
    return [_consult_span(g, advice) for g in groups]


def _consult_span(g: dict[str, dict], advice: dict[str, dict]) -> Span:
    start = g["start"]
    t0 = s_to_ns(start["ts"])
    if start["type"] == "budget_exhausted":
        attrs = {"consults_used": start["consults_used"], "limit": start["limit"]}
        return Span("advisor: budget exhausted", "CHAIN", t0, t0, attributes=attrs)
    if start["type"] == "consult_refused":
        attrs = {"reason": start["reason"], "turn": start["turn"]}
        return Span("advisor: consult refused", "CHAIN", t0, t0, attributes=attrs)
    intervention = start.get("intervention", "consult")
    brief, request = g.get("brief_built"), g.get("advisor_request")
    error, response = g.get("advisor_error"), g.get("advisor_response")
    answer = response or error
    applied = g.get("advice_applied")
    record = advice.get(request["request_id"], {}) if request else {}
    children: list[Span] = []
    attributes: dict[str, Any] = {"intervention": intervention}
    if brief:
        stats = {k: brief[k] for k in ("level", "tokens", "identifiers_redacted", "role_map_size")}
        attributes |= stats
        t = s_to_ns(brief["ts"])
        end = s_to_ns(request["ts"]) if request else t
        text = clip(request["brief_text"]) if request else None
        children.append(Span("brief", "CHAIN", t, end, outputs=text, attributes=stats))
    if request:
        t = s_to_ns(request["ts"])
        attributes |= {"request_id": request["request_id"], "prompt_hash": request["prompt_hash"]}
        children.append(
            Span(
                "advisor",
                "CHAT_MODEL",
                t,
                s_to_ns(answer["ts"]) if answer else t,
                inputs=clip({"system": record.get("system"), "brief": request["brief_text"]}),
                outputs=clip(record.get("advice")),
                attributes={
                    "tokens.input": request["input_tokens"],
                    "tokens.output": (response or {}).get("output_tokens", 0),
                    "tokens.cache_read": (response or {}).get("cached_tokens", 0),
                    "latency_ms": (response or {}).get("latency_ms"),
                },
                error=error["message"] if error else None,
            )
        )
    if applied:
        attributes["applied_turn"] = applied["turn"]
    last = applied or answer or request or brief or start
    return Span(
        f"advisor: {intervention}",
        "CHAIN",
        t0,
        s_to_ns(last["ts"]),
        inputs={"reason": start.get("reason"), "turn": start.get("turn")},
        outputs=clip(record.get("injected")) if applied else None,
        attributes=attributes,
        error=error["message"] if error else None,
        children=children,
    )


NEST_SKEW_NS = 100_000_000
"""How much earlier than its parent a nested span may start: pi's extension handlers run
asynchronously, so the timeline's `tool_start` mark can land a few ms after the consult tool's
first event."""


def nest(spans: list[Span], extra: list[Span]) -> list[Span]:
    """`extra` spans placed under the deepest span that was open when each started (a consult
    tool call inside its tool span), else among `spans`; siblings in start order. A span that
    starts just before a sibling (within NEST_SKEW_NS) and inside no other goes under it."""
    for span in extra:
        siblings = spans
        while True:
            t = span.start_ns
            parent = next((s for s in siblings if s.start_ns <= t <= s.end_ns), None) or next(
                (s for s in siblings if s.start_ns - NEST_SKEW_NS <= t <= s.end_ns), None
            )
            if parent is None:
                break
            siblings = parent.children
        siblings.append(span)
        siblings.sort(key=lambda s: s.start_ns)
    return spans
