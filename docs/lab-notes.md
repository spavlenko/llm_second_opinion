# Lab notes

Short dated observations; newest first. Smoke runs are not results.

## 2026-10-03 — first real-model smoke runs

- Calibration (1 seed, dev): A0 9/24 (38%), 7 of its 15 failures don't compile. A4 6/8 valid; 16 A4 runs lost to Kimi quota (HTTP 429). Signal tasks so far 3/8 → rerun A4 on the 16 before deciding.
- Old code scored the 429s as A4 `crash` (unresolved): A4 would have read ~25%, not ~75% → why outages must be infra (fixed since).
- CI green on Linux only after 2 fixes: proxy bind address, then the preflight's hard-coded 127.0.0.1.
- CI (Linux) failed: proxy bound 127.0.0.1, unreachable via host.docker.internal there → bind the bridge gateway on Linux. Plugin e2e failed once locally, not reproduced in 2 reruns.
- Redaction leaked: names redacted in one brief section sent raw in another (`#define BENCHMARK` next to `<macro_1>`), project name never redacted (`simdjson`) → final role-map sweep + `<project_N>`.
- Grader stricter than SWE-bench: executor edited a test file, test patch then failed to apply (Catch2-1616 `test_patch_failed`) → reset test-patch files to base before applying.
- Executor edits existing tests despite the prompt; track as a metric.
- H at L2 resolved simdjson-524 (gold patch ~7000 lines) and json-18 on first try.
- Grader calibrated on dev smoke subset: gold 3/3 resolve; empty and no-op patches 3/3 fail (0 F2P passing).
- A4 crashed on every item: Kimi rejects pi's `developer` role (HTTP 400) → `supportsDeveloperRole: false` for pi-advisor. Recorded as done/unresolved: A4 would have read 0%.
- Toy tasks can't separate levels: all of L0/L2/L3 resolved; L0 turns the issue into "[code] prints 2".
- Both endpoints stream `usage` incl. reasoning; reasoning is most of the completion.
- Advice cut off at `max_answer_tokens` (400/400, mid-sentence), unrecorded → added `finish_reason`, budget in prompt.
- Turn-0 `plan` brief is near-empty; advice was generic (C++ parsing for a shell bug) → add post-orientation trigger.
- Redaction mislabels roles (`add` → `<variable_1>`); mapped back fine.
- Grading ≈ agent time (231 s vs 249 s; ctest 209 s, 60/60): budget pilot wall-clock for both.
- A0 solved `nlohmann__json-2332` alone (13 turns, 249 s, `if`→`while`, also patched `single_include`): no signal there.
- Executor runs tests as `run-tests | tail`, hiding the exit code; check `on_test_failure` fallback.
