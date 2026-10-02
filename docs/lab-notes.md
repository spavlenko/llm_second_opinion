# Lab notes

Short dated observations; newest first. Smoke runs are not results.

## 2026-10-03 — first real-model smoke runs

- Both endpoints stream `usage` incl. reasoning; reasoning is most of the completion.
- Advice cut off at `max_answer_tokens` (400/400, mid-sentence), unrecorded → added `finish_reason`, budget in prompt.
- Turn-0 `plan` brief is near-empty; advice was generic (C++ parsing for a shell bug) → add post-orientation trigger.
- Redaction mislabels roles (`add` → `<variable_1>`); mapped back fine.
- Grading ≈ agent time (231 s vs 249 s; ctest 209 s, 60/60): budget pilot wall-clock for both.
- A0 solved `nlohmann__json-2332` alone (13 turns, 249 s, `if`→`while`, also patched `single_include`): no signal there.
- Executor runs tests as `run-tests | tail`, hiding the exit code; check `on_test_failure` fallback.
