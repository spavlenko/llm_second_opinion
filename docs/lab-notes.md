# Lab notes

Dated observations worth keeping for the paper and for the next design decision: anything
interesting, unexpected, or surprising, with the evidence that shows it. Newest first. Smoke
and devtest runs are not results (they are not on a frozen protocol); they say what to look at.

Each entry: what was seen, the evidence (run, file, numbers), why it matters, and what was done.

## 2026-10-03: first runs with the real models

Runs: `smoke-a0` (A0 on `nlohmann__json-2332`) and `smoke-advisor` (H at L2 with `plan` and
`consult` on `toy-add`), both in MLflow and under the session scratch directory. Not results.

**Both endpoints report usage in streams, including reasoning tokens.** The preflight passed
for the executor (`qwen3.8-27b`: 17 prompt / 22 completion / 19 reasoning tokens, 0.7 s) and
the advisor (`kimi-k3`: 92 / 58 / 42, 2.9 s). Reasoning is most of the completion on both, so
cost and answer caps have to count it.

**The advisor's answer was cut off by its own cap.** `max_answer_tokens: 400` was sent as
`max_tokens`; the answer used exactly 400 completion tokens (43 of them reasoning) and ended
mid-sentence ("5. Fix and confirm. Make the"). The advisor's prompt did not state the budget,
and nothing recorded the truncation. *Action:* `finish_reason` added to `advisor_response`,
truncated advice marked for the executor, and the budget stated in the default
`advisor_system` prompt.

**A plan consult at turn 0 carries almost no information.** The brief had the issue and
empty "tried", "output", and "code" sections, and Kimi answered with a generic C++ plan
(`std::cin` parsing, stub functions) for what is a one-line shell bug. The executor checked it
against the code and fixed the bug anyway (10 turns, 18 s). This matches the advisor-tool
guidance in [related-work.md](related-work.md): consulting before the executor has context
can hurt. *Action:* a "plan after orientation" trigger, and empty brief sections dropped.

**Redaction labels roles by guesswork.** `add` (a shell function) became `<variable_1>`. The
placeholder was restored correctly before injection, so only the role is wrong; the leakage
scorer should still measure what a role label gives away.

**The executor alone solved a real task quickly.** A0 on `nlohmann__json-2332` (lexer skips
only one comment in a row) found the bug by reading `lexer.hpp`, fixed it (`if` → `while`), and
also patched the amalgamated `single_include/nlohmann/json.hpp`, in 13 turns and 249 s, with
65k prompt tokens (47k of them cached by the server). A task A0 solves reliably says nothing
about help policies (the gap A4 − A0 is zero there); the pilot's per-task A0 rate shows which
tasks carry signal.

**The executor pipes the test run through `tail`.** It ran `/opt/lso/run-tests 2>&1 | tail -30`,
which hides the exit code. The plugin's `on_test_failure` trigger already falls back to
parsing the ctest summary for this reason; worth confirming on the first real failing run.
