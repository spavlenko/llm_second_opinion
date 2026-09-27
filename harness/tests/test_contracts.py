import json

import pytest
from pydantic import ValidationError

from llm_second_opinion.contracts import (
    EVENT_ADAPTER,
    AdvisorRequest,
    AgentResult,
    parse_events,
    render_schemas,
    stale_schemas,
)


def test_committed_schemas_match_models(repo):
    assert stale_schemas(repo / "schemas") == [], "run `bench schemas` to regenerate"


def test_every_property_required_so_discriminator_is_mandatory():
    event = json.loads(render_schemas()["event.schema.json"])
    for name, definition in event["$defs"].items():
        if "properties" not in definition:
            continue  # enums
        assert "type" in definition["required"], name
        assert definition["required"] == list(definition["properties"])


def test_events_round_trip():
    records = [
        {
            "seq": 0,
            "ts": 1.5,
            "type": "trigger_fired",
            "intervention": "stuck",
            "reason": "same error x3",
            "turn": 12,
        },
        {
            "seq": 1,
            "ts": 2.0,
            "type": "advisor_request",
            "request_id": "r1",
            "input_tokens": 900,
            "brief_text": "the brief",
        },
    ]
    lines = json.dumps(records[0]) + "\n\n" + json.dumps(records[1])
    events = parse_events(lines)
    assert [e.type for e in events] == ["trigger_fired", "advisor_request"]
    assert isinstance(events[1], AdvisorRequest)
    assert events[1].brief_text == "the brief"
    again = EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(events[0]))
    assert again == events[0]


@pytest.mark.parametrize(
    "line",
    [
        '{"seq": 0, "ts": 1, "type": "made_up"}',
        '{"seq": 0, "ts": 1, "type": "budget_exhausted", "consults_used": 5}',
        '{"seq": 0, "ts": 1, "type": "budget_exhausted", "consults_used": 5, "limit": 5, "x": 1}',
        '{"seq": -1, "ts": 1, "type": "budget_exhausted", "consults_used": 5, "limit": 5}',
    ],
)
def test_bad_events_rejected(line):
    with pytest.raises(ValidationError):
        parse_events(line)


def test_result_rejects_unknown_exit_reason():
    with pytest.raises(ValidationError):
        AgentResult.model_validate(
            {
                "diff": "",
                "exit_reason": "gave_up",
                "agent": {"name": "pi", "version": "1"},
                "turns": 3,
                "duration_s": 1.0,
            }
        )
