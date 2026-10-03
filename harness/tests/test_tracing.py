"""Advisor spans from the plugin's events.jsonl and advice.jsonl."""

from llm_second_opinion.tracing import Span, advisor_spans, nest

S = 1_000_000_000


def ev(ts: float, type: str, **fields) -> dict:
    return {"schema_version": "1", "seq": 0, "ts": ts, "type": type} | fields


EVENTS = [
    ev(10.0, "consult_requested", reason="why?", turn=1),
    ev(10.1, "brief_built", level="L2", tokens=50, identifiers_redacted=3, role_map_size=3),
    ev(10.2, "advisor_request", request_id="r1", input_tokens=80, brief_text="B", prompt_hash="h"),
    ev(
        12.0,
        "advisor_response",
        request_id="r1",
        output_tokens=9,
        cached_tokens=0,
        latency_ms=1,
        advice_text="a",
    ),
    ev(12.5, "advice_applied", request_id="r1", turn=1, injected_text="a"),
    ev(20.0, "trigger_fired", intervention="stuck", reason="same_error", turn=4),
    ev(20.1, "brief_built", level="L2", tokens=40, identifiers_redacted=1, role_map_size=4),
    ev(20.2, "advisor_request", request_id="r2", input_tokens=70, brief_text="B2", prompt_hash="h"),
    ev(21.0, "advisor_error", request_id="r2", message="HTTP 403: token_limit"),
    ev(30.0, "budget_exhausted", consults_used=2, limit=2),
]
ADVICE = {"r1": {"request_id": "r1", "system": "S", "advice": "A", "injected": "I: A"}}


def test_one_span_per_consult_with_brief_and_advisor_call():
    consult, stuck, budget = advisor_spans(EVENTS, ADVICE)
    assert (consult.name, consult.start_ns, consult.end_ns) == (
        "advisor: consult",
        10 * S,
        12.5 * S,
    )
    assert (consult.inputs, consult.outputs) == ({"reason": "why?", "turn": 1}, "I: A")
    assert consult.attributes["applied_turn"] == 1
    assert consult.attributes["identifiers_redacted"] == 3
    brief, call = consult.children
    assert (brief.name, brief.outputs) == ("brief", "B")
    assert (call.kind, call.inputs, call.outputs) == (
        "CHAT_MODEL",
        {"system": "S", "brief": "B"},
        "A",
    )
    assert call.attributes["tokens.output"] == 9
    assert (stuck.name, stuck.error, stuck.outputs) == (
        "advisor: stuck",
        "HTTP 403: token_limit",
        None,
    )
    assert stuck.children[1].error == "HTTP 403: token_limit"
    assert (budget.name, budget.attributes) == (
        "advisor: budget exhausted",
        {"consults_used": 2, "limit": 2},
    )


def test_spans_nest_under_the_deepest_span_open_when_they_start():
    tool = Span("consult", "TOOL", 9 * S, 13 * S)
    turn = Span("turn 1", "CHAIN", 8 * S, 14 * S, children=[tool])
    later = Span("turn 2", "CHAIN", 22 * S, 25 * S)
    top = nest([turn, later], advisor_spans(EVENTS, ADVICE))
    names = [s.name for s in top]
    assert names == ["turn 1", "advisor: stuck", "turn 2", "advisor: budget exhausted"]
    assert [s.name for s in tool.children] == ["advisor: consult"]


def test_a_span_starting_just_before_its_tool_still_nests_under_it():
    # pi's timeline marks tool_start from an async handler, a few ms after the tool's own work.
    early = Span("advisor: consult", "CHAIN", 9 * S - 5_000_000, 9 * S + 1)
    gap = Span("advisor: stuck", "CHAIN", 9 * S - S, 9 * S - S)
    tool = Span("consult", "TOOL", 9 * S, 13 * S)
    turn = Span("turn 1", "CHAIN", 7 * S, 14 * S, children=[tool])
    nest([turn], [early, gap])
    assert [s.name for s in tool.children] == ["advisor: consult"]
    assert [s.name for s in turn.children] == ["advisor: stuck", "consult"]


def test_a_refused_consult_is_a_span_of_its_own():
    [span] = advisor_spans([ev(5.0, "consult_refused", reason="min_own_actions", turn=0)])
    assert (span.name, span.start_ns, span.attributes) == (
        "advisor: consult refused",
        5 * S,
        {"reason": "min_own_actions", "turn": 0},
    )
