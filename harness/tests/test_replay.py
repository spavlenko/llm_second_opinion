import json

import pytest

from llm_second_opinion.replay import NEUTRAL, NoFork, build, recording


def assistant(*calls, text="", thinking="hm"):
    content = [{"type": "thinking", "thinking": thinking}]
    content += [{"type": "text", "text": text}] if text else []
    content += [{"type": "toolCall", "id": i, "name": n, "arguments": {"x": 1}} for i, n in calls]
    return {"type": "message_end", "message": {"role": "assistant", "content": content,
            "stopReason": "toolUse" if calls else "stop", "usage": {"input": 10, "output": 2}}}  # fmt: skip


def result(call_id, text):
    return {"type": "message_end", "message": {"role": "toolResult", "toolCallId": call_id,
            "content": [{"type": "text", "text": text}]}}  # fmt: skip


def start(call_id, name):
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": {}}


SYSTEM = {"type": "message_end", "message": {"role": "system", "content": "",
          "sections": {"addendum": "<addendum>\nInvestigate first.\n</addendum>"},
          "toolsAdded": [{"name": "consult", "description": "Ask.", "parameters": {"type": "object"}}]}}  # fmt: skip


def run_dir(tmp_path):
    pi = [
        SYSTEM,
        assistant(("e1", "edit")), start("e1", "edit"),
        result("e1", "This edit was not applied: before your first change, file your report"),
        {"type": "turn_end"},
        assistant(("c1", "consult")), start("c1", "consult"), result("c1", "Advice one."),
        {"type": "turn_end"},
        assistant(text="Fixed."),
        {"type": "entry_appended", "entry": {"customType": "lso-advice", "content": "Before you finish, file your closing report"}},
        {"type": "turn_end"},
        assistant(("c2", "consult")), start("c2", "consult"), result("c2", "Done."),
        {"type": "turn_end"},
    ]  # fmt: skip
    events = []
    for i in (1, 2):
        events += [{"type": "consult_requested", "turn": i},
                   {"type": "advisor_request", "request_id": f"r{i}"},
                   {"type": "advisor_response", "request_id": f"r{i}", "advice_text": "a"}]  # fmt: skip
    (tmp_path / "pi.jsonl").write_text("".join(json.dumps(e) + "\n" for e in pi))
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return tmp_path


def test_first_consult_fork_serves_the_log_up_to_it(tmp_path):
    recordings, script = build(run_dir(tmp_path), 1, "neutral")
    assert len(recordings) == 2  # the refused edit, then the consult call
    assert script["fork"] == "c1" and script["answers"] == {"c1": NEUTRAL}
    assert script["refused"] == {
        "e1": "This edit was not applied: before your first change, file your report"
    }
    assert script["appended"] == {} and script["guidance"] == "Investigate first."


def test_last_and_trigger_forks(tmp_path):
    recordings, script = build(run_dir(tmp_path), "last", "advice")
    assert len(recordings) == 4 and script["fork"] == "c2"
    assert script["answers"] == {"c1": "Advice one.", "c2": "Done."}
    assert script["appended"] == {"3": ["Before you finish, file your closing report"]}
    assert build(tmp_path, "closing", "advice")[1]["fork"] == "c2"
    with pytest.raises(NoFork):
        build(tmp_path, "stuck", "advice")


def test_recording_carries_thinking_and_calls():
    r = recording(assistant(("t1", "bash"), text="Let me look.")["message"])
    assert r["message"]["reasoning_content"] == "hm" and r["message"]["content"] == "Let me look."
    assert r["message"]["tool_calls"][0]["function"] == {"name": "bash", "arguments": '{"x": 1}'}
    assert r["usage"]["prompt_tokens"] == 10
