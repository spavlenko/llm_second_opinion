from llm_second_opinion.consult_value import consult_rows, summary, verdict


def call(name, **args):
    return {"type": "tool_execution_start", "toolName": name, "args": args}


def result(text):
    return {
        "type": "message_end",
        "message": {"role": "toolResult", "content": [{"type": "text", "text": text}]},
    }


def appended(text):
    return {"type": "entry_appended", "entry": {"type": "custom_message", "content": text}}


def consult_events(*answers):
    out = []
    for i, advice in enumerate(answers, 1):
        out += [
            {"type": "consult_requested", "turn": i * 10},
            {"type": "advisor_request", "request_id": f"r{i}", "input_tokens": 1000},
            {"type": "advisor_response", "request_id": f"r{i}", "prompt_tokens": 1200, "output_tokens": 300,
             "advice_text": advice},
        ]  # fmt: skip
    return out


def test_each_consult_gets_its_trigger_edits_and_verdict():
    pi = [
        call("edit", path="a.hpp"),
        result(
            "This edit was not applied: before your first change, file your investigation report"
        ),
        call("consult"),
        call("edit", path="a.hpp"),
        call("edit", path="b.hpp"),
        call("consult"),  # asked on its own
        call("bash", command="sed -i s/x/y/ a.hpp"),
        appended("Before you finish, file your closing report with the `consult` tool."),
        call("consult"),
    ]
    rows = consult_rows(consult_events("cause A", "try B", "**Strongest reason:** none. Done."), pi)
    assert [(r["trigger"], r["edits_after"], r["new_files_after"]) for r in rows] == [
        ("report_gate", 2, 1),  # b.hpp is new; a.hpp was touched before (the refused edit)
        ("own", 1, 0),
        ("closing", 0, 0),
    ]
    assert rows[2]["verdict"] == "done" and rows[0]["verdict"] is None
    assert rows[0]["kimi_in"] == 1200 and rows[0]["turn"] == 10
    assert (
        verdict("**Rejection reason:** stale header.\n\nNot Done until regenerated.") == "not done"
    )
    assert verdict("Almost — one thing missing.") == "not done"
    lines = summary(
        [{**r, "arm": "H", "resolved": True, "right": None, "followed": None} for r in rows]
    )
    assert lines[0] == "H: 3 consults" and "closing: done & resolved 1" in lines[-1]
