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
- 2026-10-08: review on gate-a0 (first 26 groups): Kimi picked the resolving patch in 4 of 10 groups where the shown candidates differ (random ≈ 5). In 6 of the 10 the losers build and pass visible tests locally but fail to *build* against the hidden tests, which call the API the upstream fix added (e.g. `Catch2-2849`: Kimi preferred a "surgical" fix over the one adding `AnsiSkippingString`). Diff review can't see that.
- 2026-10-08: the gate-a0 restart script's `docker rm -f` on every labelled container also killed the concurrent `bench pick`'s local checks (409 "removal already in progress"); filter on `llm-second-opinion=<kind>`.
- 2026-10-08: gate-a0 `L-best3` on 74 groups: oracle 38, random 19.7, local 25, builds-then-random 27, Kimi review 28 (consulted in 68). Gap to A4 (68.7) closed: 7%; a perfect picker would close 30%. Kimi: 70 calls, 471k in, 156k out. `fmt-3248` s1 times out on every retry.
- 2026-10-08: gate-phase (H-phase, 75 runs): 25 resolved vs A0's 19.7 expected (9-seed rates) and `L-best3`'s 25. Kimi: 195 consults (49 runs used all 3), 172k in, 126k out. One hinted run ≈ three unhinted runs plus a local picker. Gate: no-go.
- 2026-10-08: gate-phase briefs (195): Kimi never saw a command's output, the executor's thinking (pi `thinking` parts were dropped; Qwen writes almost no plain text, so `orient` notes appeared in 11/65) or its edits; median ~1k chars beyond the issue. On `json-3601` Kimi said "check the output of your `git log`" and flipped between plan and orient.
- 2026-10-08: with the evidence brief (5–17k chars), Kimi still told all 3 `json-3601` seeds to fix the examples (the upstream fix restores `operator<<`), once claiming "the upstream fix did exactly that". A "restore the behaviour the issue shows" rule flipped all 3 to the library fix; 2 of them then printed the pointer in quotes and failed.
- 2026-10-08: stopping `bench pick` (SIGTERM) left its 3 check containers running for hours; 6 orphans found. Check `docker ps` after the process exits.
- 2026-10-08: gate-evidence stopped at 11:37: Kimi's weekly (7-day) plan quota ran out (HTTP 403 `access_terminated_error`) after ~50 consults this run. 4 affected runs (json-2297/2352/2989/3463 s0) marked failed in the ledger (backup `ledger.sqlite.bak-20261008-quota`). Pi carries on without advice on a failed consult, so a quota cut silently turns H into A0; `bench run` now fails such an attempt (retried) and stops the batch on a 401/403.
- 2026-10-08: H-evidence partial, 18 clean runs on 14 tasks: 8 resolved vs A0 6.8 expected and H-phase 6.0 (per-task rates). Too few to call; wins on fmt-1407 and json-3605 (A0 0.11, 0.22), losses on fmt-3248 and json-3590 (H-phase 0.67).
- 2026-10-08: stopping `bench run` (SIGTERM) also leaves its task containers (`sleep infinity`) running; 3 removed.
- 2026-10-08: gate-evidence final (resumed on Kimi Extra Usage, cut again by the weekly-limit 403 at ~23:15; the new guard stopped the batch after 1 failed run): 61 done, 19/60 paired vs `L-best3` 23, H-phase 20, A0 16.4 expected. −12% of the gap (CI −46% .. 11%). Richer briefs gave better diagnoses but not more resolves. 154 consults, 348k in / 173k out.
- 2026-10-08: failures over A0 + H runs: 110 build_failed, 120 tests_failed (103 resolved, 28 empty). The build failures are hidden tests calling names the issue never gives (`compute_float_boundaries`, `AnsiSkippingString`, `catch_test_case_info_hasher`, `JSON_TESTS_PRIVATE`); A4's patches use exactly those names, i.e. Kimi recalls the upstream fix. The A4 ceiling is partly memorization, which private code would not get.
- 2026-10-08: gate split by task type (interface: ≥75% of failures are build failures, 10 tasks). Interface: H-evidence 5/25, L-best3 8, A0 5.8; no help at all. Logic (15 tasks): H-evidence 14/35 vs L-best3 15, A0 10.7 (+15% of the A0–A4 gap); H-phase 18/44 vs 15 (+12% vs L-best3). Hints only help where the fix needs no hidden name.
- 2026-10-08: what Kimi got in gate-evidence (170 consults). plan (68, 40%): the issue only (92% of the brief) and a fixed question, nothing from Qwen. orient/stuck: 64% of the brief is raw read/grep output (503 of 540 commands shown are reads; 37 builds or tests), 12% reasoning (median 700 chars), edits in 14 of 102, the executor's own hypothesis in 0, its own question in 0 (all 3 questions are templates).
- 2026-10-08: brief bug: "Latest build or test output" is the last call that *failed*, and a grep with no match counts as failed. In 102 orient/stuck briefs it held a real test result 3 times, a compiler error 12, a grep/ls/"(no output)" 32, and the source of the test script 18: `cat /opt/lso/run-tests` matches `testRun` and the script contains "run-tests: build failed", so it reads as a failed build and stays the "latest failure" for the rest of the run.
- 2026-10-09: loop-case H-case, first 6 runs: the report gate works (every run reports before its first edit, ~800-token briefs, no code), Kimi's answers fit the format, Qwen reads back. But Qwen consults once and never returns (0 follow-ups). fmt-3248: upstream fix plus a float special case to keep `{:>06.0f}` == "000000", a visible test the hidden patch changes. simdjson-644: Kimi says make `operator[]` a key lookup (upstream's fix); Qwen recalls "operator[] has always been a JSON pointer" and only adds `at_pointer`. json-2019 resolved after Qwen asked whether a contradicting test would be updated.
- 2026-10-09: Qwen stall: 44.5k reasoning tokens in one call, then a stop with no text and no tool call; pi ends the run (json-2225, turn 3, empty patch). Not a context limit (49k of 140k) nor a server cap (other calls reach 60k). 12 of 14.5k earlier executor calls generated >30k tokens.
