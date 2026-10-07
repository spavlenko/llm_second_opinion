# Lab notes

Short dated observations; newest first. Smoke runs are not results.

## 2026-10-07 — screening

- Screening, 36 new `dev` tasks: A0 40/108; 18 new signal tasks (25 in all). A4 seed 1 failed after a seed-0 solve in 2 of 15 tasks (fmt-1361 ended 2/3, fmt-3727 1/3) → one A4 seed would have mis-selected fmt-3727.
- A4 0/1 where A0 also failed on 6 tasks (Catch2-1614, fmt-3819, json-2576, json-3514, simdjson-543, fmt-3912 at A0 1/3): no headroom there for any advisor.

- A4 runs on the 25 gate tasks that saw a broken build in run-tests: 4/62 (3 resolved): the run-tests fix hardly touches the ceiling.
- Any advisor-core change that reaches the bundled `lso-advisor.js` changes the pi bundle image and so A0's config hash too (A0 has no advisor): a refactor of brief.ts would have orphaned gate-a0's 225 runs for `bench pick`. Kept the bundle byte-identical; hash checked unchanged.
- Plugin e2e test failed once alone, right after a bundle rebuild (`consult` span had no child); passed 3 reruns. Still flaky.

## 2026-10-06 — full C++ set

- `full` C++: 78 new instances built and validated on arm64, 74 kept; dropped fmt-3863 (flaky format-test), fmt-3279 and fmt-3260 (gold fails chrono-test, as fmt-3271 in mini), simdjson-2150 (gold fails `simdjson_force_implementation_error`).
- Uncertainty, 145 A0 runs (46 tasks; calib-floor left out, older layout): the executor's own test verdict misses failures, since the fail-to-pass tests are hidden: of 107 runs it calls fine, 59 fail. Best single signal is reasoning tokens (AUC 0.70, task-bootstrap CI 0.59-0.80); turns, patch size ~0.62.
- Pick one of 3 seeds (47 tasks): oracle 28, random 17, best local rule (least reasoning) 21, own tests then medoid 15. Local selection recovers 4 of 11 → a review consult that picks among local patches targets the rest.
- Patch similarity across seeds: 0.67 when 3/3 resolve, 0.30 at 1/3, 0.41 at 0/3 (consistent wrong fixes).
- The executor pipes `run-tests | tail`: exit code lost and the build-failed notice cut; only ctest's summary line survives.
- A4 prompt tokens: mean 747k/run (94% cached), max 3.7M (simdjson-543, 62 calls × ~59k context): whole-header reads repeated 3-5× (51k-char `read` cap) stay in context and are resent each turn. Kimi's 5-h window held ~20-24 A4 runs.
- Image tags are per instance, not per set: building mini IDs under another set would retag over images that v2 pins by ID.

## 2026-10-05 — headroom

- Headroom (9 candidates, 3 seeds, current code): 7 signal tasks (< 8) → rule says switch set. Dropped: json-1138 (A0 2/3), json-3664 (A4 0/3 after a 1-seed solve). A4 27/27 elsewhere; A0 turn_limit 2/27. One-seed screens mislead both ways.
- On the 7 signal tasks A0 resolves 4/21 runs but 4/7 tasks in some seed: selection, not generation, is half the gap.
- Unsolved `build_failed` grades (both arms) look like hidden-test interface mismatches (`benchmarkNoAnalysis`, `compares_unordered`), not compile slips; repro not yet run.

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
- 2026-10-07: gate-a0 died of a full disk: in `json-3543` s0 the agent's repro looped printing one parse error; pi's bash output spill (`/tmp/pi-bash-*.log`) hit 109 GB in 18 min. Task containers now cap any file at 4 GiB (`--ulimit fsize`, SIGXFSZ to the writer); the 4 infra-failed items rerun on resume.
- 2026-10-07: review smoke on calib-noise (L3, Kimi): `json-3664` 3 distinct patches, Kimi ranked the only resolving one first (4.2k in, 3.5k out, 98 s); `fmt-2394` 3 seeds, one patch → no consult.
- 2026-10-07: `bench run` started with `nohup … &` ignores SIGINT (async jobs of a non-interactive shell inherit SIG_IGN), so no graceful stop; SIGTERM, then the resume regrades or reruns what was in flight. `--parallel` is not in the config hash.
- 2026-10-07: executor server at 6 parallel pi agents: prompt-cache hit 94% → 19%, p50 call 1.3 s → 23 s, total throughput no higher than at 3; the cache holds ~3–4 agent contexts. Max prompt seen 119k (limit 165k). Back to 4.
- 2026-10-07: the 4 GiB file cap fired: `json-3543` s4 looped again (pi exited 153, SIGXFSZ; graded as `crash`, its patch kept). That task loops in 2 of 5 seeds so far.
