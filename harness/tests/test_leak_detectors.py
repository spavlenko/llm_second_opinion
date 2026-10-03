"""Planted-leak calibration of the two leak detectors: `scripts/smoke-check.py` (`brief_leaks`)
and the scorers (`role_map_leaks`, `leaked_units`). Known names are planted raw in synthetic
briefs; each detector's hits and misses are asserted exactly. A surrogate-style fake name
and fully redacted briefs must stay quiet."""

import importlib.util
import json
from pathlib import Path

import pytest

from llm_second_opinion.scorers import leaked_units, role_leaks, role_map_leaks

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("smoke_check", ROOT / "scripts/smoke-check.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

GOLD = """\
diff --git a/include/fmt/format-inl.h b/include/fmt/format-inl.h
index 1..2 100644
--- a/include/fmt/format-inl.h
+++ b/include/fmt/format-inl.h
@@ -40,7 +40,7 @@ int parse_format_specs(const char* p)
-  if (FMT_LIKELY(*p == '-')) return 1;
+  while (FMT_LIKELY(*p == '-')) return 1;
"""
FUNCTION, MACRO, PATH = "parse_format_specs", "FMT_LIKELY", "include/fmt/format-inl.h"
# As the plugin builds it for fmtlib__fmt-N: both project names, then what the briefs held.
ROLE_MAP = {"<project_1>": "fmtlib", "<project_2>": "fmt", "<function_1>": FUNCTION,
            "<macro_1>": MACRO, "<file_1>": PATH}  # fmt: skip

# case: (brief, follow-up or None, role-map hits (both role-map detectors), leaked_units hits)
CASES = {
    "function": ("The bug is in parse_format_specs() for a negative width.", None,
                 [FUNCTION], [FUNCTION]),
    "function-member": ("It calls ctx.parse_format_specs(p) twice.", None,
                        [FUNCTION], [FUNCTION]),
    "path": (f"See {PATH} near line 40.", None, ["fmt", PATH], [PATH]),
    "file-name": ("The change belongs in format-inl.h.", None, [PATH], [PATH]),
    "macro": ("Wrap the check in FMT_LIKELY.", None, [MACRO], [MACRO]),
    "project": ("This is the fmt library's formatter.", None, ["fmt"], []),
    "project-case": ("In FmtLib, width parsing is strict.", None, ["fmtlib"], []),
    "follow-up": ("<function_1> rejects a negative width.",
                  "int parse_format_specs(const char* p) {", [FUNCTION], [FUNCTION]),
    # Must stay quiet: a surrogate (a plausible fake name), placeholders, plain words.
    "surrogate": ("The bug is in decode_width_field() in <file_1>; <macro_1> wraps it.", None,
                  [], []),
    "clean": ("<function_1> in <file_1> of <project_2> parses the width; the parse loop stops.",
              None, [], []),
}  # fmt: skip


def events(brief: str, followup: str | None) -> list[dict]:
    out = [
        {"type": "policy_rendered", "seq": 0, "ts": 1.0, "prompt_hash": "h",
         "executor_guidance": "g", "consult_tool": None},
        {"type": "brief_built", "seq": 1, "ts": 1.0, "level": "L2", "tokens": 10,
         "identifiers_redacted": 3, "role_map_size": len(ROLE_MAP), "truncated": False,
         "role_map": ROLE_MAP},
        {"type": "advisor_request", "seq": 2, "ts": 1.0, "request_id": "r1", "input_tokens": 10,
         "brief_text": brief, "prompt_hash": "h", "history_turns": 0},
    ]  # fmt: skip
    if followup is not None:
        out.append({"type": "advisor_followup", "seq": 3, "ts": 1.0, "request_id": "r1",
                    "requested": "the definition of <function_1>", "sent_text": followup,
                    "tokens": 12})  # fmt: skip
    return out


def smoke_hits(problems: list[str]) -> list[str]:
    names: set[str] = set()
    for p in problems:
        names.update(json.loads(p.split(" contains ", 1)[1].replace("'", '"')))
    return sorted(names)


@pytest.mark.parametrize("case", CASES)
def test_planted_leaks(case):
    brief, followup, role_hits, unit_hits = CASES[case]
    evs = events(brief, followup)
    assert smoke_hits(smoke.brief_leaks(evs, "L2")) == sorted(role_hits)
    assert role_map_leaks(evs) == sorted(role_hits)
    sent = [brief] + ([followup] if followup is not None else [])
    _, leaked, units = leaked_units(sent, GOLD)
    assert units == 3 and leaked == sorted(unit_hits)


def test_l3_is_not_checked_by_the_smoke_check():
    assert smoke.brief_leaks(events(CASES["function"][0], None), "L3") == []


def test_smoke_check_reports_a_planted_leak_end_to_end(tmp_path):
    item = tmp_path / "H/fmtlib__fmt-1/attempt-1"
    item.mkdir(parents=True)
    (item / "result.json").write_text(json.dumps({"exit_reason": "finished", "turns": 3}))
    (item / "advisor.json").write_text(json.dumps({"advisor": {"level": "L2"}}))
    evs = events("x" * 130 + " FMT_LIKELY", "see format-inl.h")
    (item / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    problems = smoke.check(item)
    assert f"r1: L2 brief contains ['{MACRO}']" in problems
    assert f"r1: L2 follow-up contains ['{PATH}']" in problems


def test_a_directory_name_is_not_a_leak_on_its_own():
    # Smoke round 5: `/testbed/include` was flagged because briefs say `#include`.
    assert not role_leaks("<file_6>", "/testbed/include", "#include <vector> and include/x")
    assert role_leaks("<file_6>", "/testbed/include", "ran ls /testbed/include today")
    assert role_leaks("<file_2>", "include/simdjson.h", "see simdjson.h")
