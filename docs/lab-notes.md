# Lab notes

Short dated observations; newest first. Smoke runs are not results.

## 2026-10-04 — calibration

- A4 rerun on the 16 quota-lost tasks via Kimi Code: 12/16, no infra errors. Calib totals (1 seed): A0 9/24 (38%), A4 18/24 (75%); 9 candidate signal tasks (A0 fail, A4 solve), 6 neither, 9 both.
- Calibration, noise and smoke-round data sat in the session scratchpad (temporary) → copied to `runs/` (calib-floor, calib-noise, smoke-rounds, calib-notes). Run batches only into `runs/`.

## 2026-10-03 — scorers

- Planted-leak test, misses before the fix (smoke-check / role_map_leaks / leaked_units): `ctx.parse_format_specs(` all 3; `format-inl.h` without its path 2 (role map); project `fmt`, `FmtLib`, `include/fmt/…` 2 (role map); a `clarify` follow-up all 3 (never scanned) → role-aware rule, follow-ups scanned; surrogate and clean briefs quiet before and after.
- Gold similarity by lines scored a near-miss one-line fix 0 → by characters (≤ 1.7 s on real patches up to 37k chars), by lines above 20k.
- Gold patches reach 276k chars (amalgamated `single_include` copies): units and similarity count both copies.
- `test_pi_docker` fails at `prepilot`: the bundled plugin does not emit `brief_built.truncated` yet.

## 2026-10-03 — first real-model smoke runs

- Text tool calls: 4/62 executor runs (2 of the A0 "empty patch" failures in calib) → nudge extension for every arm.
- Smoke r5 (new help flow, 1 seed): 0/3 resolved. json-18: executor emitted tool calls as text (`<tool_call><function=bash>`), server did not parse them, pi stopped at turn 3 → empty patch; the other two failed to build (within measured noise). Still 0 self-consults with the new guidance.
- Leak checker flagged `/testbed/include` via its base name `include` (false positive) → base names only with an extension; plugin left `simdjson/*.h` raw → project names swept as path components.
- Targeted tests missed a contract fixture that CI caught (`history_turns`): targeted runs are not enough after contract changes.
- Real smoke (3 dev tasks, H L2): executor never called `consult` (0/3; tool and guidance verified present) → all help was harness-triggered. `orient` fired at turn 2 every time; `stuck` (no_diff) fired twice on json-18 and once after `before_done` on simdjson-524 and used up the budget.
- Bundle volume fill raced across arms (lock was per adapter; 6/8 toy items failed as infra) → process-wide lock + flock; fresh-volume rerun 8/8. Kimi Code accepts pi's own client (A4 2/2).
- Kimi Code blocks generic clients at Cloudflare (403, code 1010 for `Python-urllib`) → truthful `llm-second-opinion/<ver>` User-Agent on our own requests; never fake one (ToS).
- Advisor moved to the Kimi Code plan (`k3`) after the API account was suspended for balance; proxy now waits out 429s.
- Plugin e2e test still flaky under full-suite load (passes alone); open.
- Noise (A0, 5 seeds): fmt-2394 5/5, json-3664 1/5 (calib single seed said "fails") → classify tasks by rates over ≥3 seeds: signal = A0 ≤ 1/3 and A4 ≥ 2/3 (rule tightened before the A4 reruns).
- Executor not deterministic: temperature 0 + fixed seed gave 2 distinct of 3 answers → seeds are replicates only; sampling set explicitly (T 0.6, top_p 0.95).
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
