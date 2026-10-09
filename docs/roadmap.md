# Roadmap

The build order from [spec.md](spec.md), at several days a week (~8 weeks to the pilot, ~11 to
confirmed results). Each step ends with something that runs. Tick items as they land and note
anything deferred.

Step 2 carries the most risk: arm64 build problems only show up during validation.

## Week 1 — skeleton, contracts, mock server

- [x] Monorepo skeleton, README, MIT license
- [x] Contract models (`contracts.py`) and generated JSON Schemas in `schemas/`, with a drift test
- [x] Experiment config: YAML + env interpolation, sweeps, validation, config hash, `run_config()`
- [x] Mock model server with replay, streaming, and upstream recording (`bench mock-server`)
- [x] Node 26 + pnpm 12 toolchain; MLflow 3.16 installed, local server script (`scripts/mlflow-server.sh`)
- [x] Generated TypeScript types from `schemas/` (`pnpm gen:types`, drift check `pnpm check:types`)
- [x] Plugin contract test: `EventWriter` output validates against `event.schema.json`
- [x] CI: ruff, pytest (Docker tests included), `bench schemas --check`, plugin build and tests

## Weeks 1–2 — task pipeline (Multi-SWE-bench C++)

- [x] Internal task format model (minimal; toy task set for harness tests)
- [x] Multi-SWE-bench C++ importer (`mini`, pinned revision; `full` registered)
- [x] arm64 image builds from per-repo recipes (pinned CMake replaces upstream's x86-64 one)
- [x] Gold-patch validation, run twice for flakiness; test lists re-derived on arm64
- [x] Frozen manifest writer, with dropped instances and reasons
- [x] First manifest (`mswe-mini-cpp-v1`: 49 tasks, dev 24 / test 25); smoke task set (3 tasks)
- [x] `mswe-mini-cpp-v2`: v1 with two rebuilt, revalidated images (the originals were deleted)
- [x] `dev`/`test` split recorded in the manifest (fixed seed, stratified by repository)
- [x] `mswe-full-cpp-v1`: `full` without cpp-httplib, 123 tasks (dev 60 / test 63); the 49 v2
      tasks keep their split (`freeze --keep-splits`), only the 74 new ones are split
- [ ] Recipe for yhirose/cpp-httplib (1 instance; low priority)
- [x] Screen the 36 new `dev` tasks in two stages (A0 3 seeds + A4 1 seed; then 2 more A4 seeds):
      18 new signal tasks, 25 in all (`runs/screen-full-a0`, `runs/screen-full-a4`)

## Week 3 — A0 end to end on smoke tasks

- [x] `runtime`: containers via Docker API (Colima or Docker Desktop), CPU and memory caps
- [x] `adapters`: protocol and `gold` adapter
- [x] `adapters`: pi adapter and agent bundle (volume); turn limit via a pi extension
- [x] Agent settings in config: `agents:` entries (adapter, version, options) with per-adapter
      option models; short form `agent: pi`; included in the config hash
- [ ] Executor prompt as a prompt slot (`executor_guidance`), replacing the adapter's fixed prompt
- [x] Metering proxy: one shared proxy, per-item routes, forced stream usage, `usage.jsonl` +
      `usage.schema.json`; secrets added by the proxy, none in containers
- [x] Usage preflight: refuse endpoints without `usage`; a call without usage fails the item
- [x] Endpoint rewriting so agents only reach models through the proxy
- [ ] Metering with Colima: check which `--proxy-host` its VM reaches (Docker Desktop checked)
- [x] `runner`: work-item expansion, parallel workers (`--parallel`), timeouts, retries
- [x] `grading`: apply patch and test patch in a fresh container, run `eval_command`
- [x] `grading`: F2P and P2P test lists (with the importer)
- [x] Endpoint headers (`headers`, secret `header_env`), `.env` loading, `.env.example`;
      pi `thinking_level_map` and `compat` options
- [x] Task selection in experiments (`split`, `task_ids`); `experiments/baselines.yaml` (A0, A4 on `dev`)
- [ ] ~~Measure the memory split between the local model and the VM~~ (moot while the executor is remote)
- [ ] Measure executor endpoint throughput at `parallel` 1, 2, 4
- [ ] First real A0 item on one `dev` task; check the trace in MLflow

## Week 4 — tracking and operations

- [x] MLflow tracking: runs per arm and item, artifacts, pinned inputs
- [x] MLflow mandatory for `bench run`; item traces from agent logs (pi); arm summary metrics and git commit
- [x] Advisor spans (triggers, briefs, consults) in the item traces
- [x] SQLite ledger and resume
- [ ] Debug mode, `bench shell`, `bench replay`
- [x] `bench report`: per-arm resolve rate with Wilson CI, exit reasons, time, turns; CSV
- [x] Token budget (`limits.max_tokens`, exit reason `token_limit`)
- [x] Tokens and cost in ledger, MLflow, and `bench report` (per arm, per resolved task);
      price table in the experiment
- [ ] Prices for the real models in `experiments/baselines.yaml`
- [x] Deterministic scorers (capability, cost): `bench score`
- [x] Paired comparisons in `bench report`: differences with paired bootstrap CI, McNemar;
      lift over A0 and share of the A0–A4 gap closed; Pareto front of resolve rate vs cost
      (front waits for the ledger's cost columns); `--pairs-csv`; paired metrics in MLflow
- [x] Test-split guard: `bench run` needs `--final` for `test` tasks (ledger `sessions`, MLflow
      tag `lso.final`); `bench report` shows variants tried and final batches

## Data retention (2026-10-03)

- [x] One directory per attempt (`seed-<n>/<config_hash>/attempt-<k>/`), starting empty;
      ledger `attempts` table (spend, status, error per attempt); counted attempt in `items`
- [x] Total spend in `bench report` (all attempts and preflight) next to the counted items'
- [x] Grading and tracking retried on their own from the saved result (also on resume);
      agent artifacts copied out however the run ends; interrupted attempts recorded
- [x] Executor endpoint outages are infrastructure errors (retried, then `failed`), not crashes
- [x] Proxy records failed calls, the attempt, the plugin's request id; `requests.jsonl` and
      the first executor request in full
- [x] Sampling parameters (`temperature`, `top_p`, `sampling_seed`) to pi's `samplingParams`
      and into the config hash
- [x] `grade.json` (F2P/P2P detail, build failure, missing tests, agent's test-file edits,
      grader version); `build_failed` grade reason; full build log kept
- [x] Grading resets test-patch files to base before the test patch (as SWE-bench);
      `bench regrade` re-grades stored patches
- [x] Config hash: manifest version, adapter fingerprint (task prompt, bundle image); task
      image in the ledger key; `item.json` provenance per attempt; git commit on every MLflow
      item run, commits list on arm runs
- [x] Ledger and CSV columns: cached and reasoning tokens, failed calls, prompt hash, level,
      interventions, grading detail; `report.json` per `bench report`
- [x] Trajectory metrics (`metrics.json`, MLflow): turns, tool calls, test runs, compactions,
      retries, context length, first edit, consult, and test run, advice-file uptake
- [ ] Regrade the calibration runs made with grader version 1 (`bench regrade`)
- [ ] Plugin emits the new event fields (`policy_rendered`, `brief_built.role_map`, advisor
      token counts and error status) and sends `X-LSO-Request-Id`; then the plugin Docker
      test passes again

## Weeks 5–6 — advisor plugin

- [x] `advisor-core`: brief builder at L2, consult budget, advisor client, exposure log, event emitter
- [x] `pi-binding`: consult tool, lifecycle hooks, advice injection, reading `advisor.json`
- [x] Plugin bundled into the pi bundle image (esbuild stage); advisor arms load it; e2e Docker
      test with the mock server for both roles
- [x] Planning review and consult tool interventions
- [ ] OTel exporter to MLflow
- [x] Prompt slots (`executor_guidance`, `consult_tool`, `brief`, `advisor_system`,
      `advice_injection`) with `prompts/default/`; placeholder validation (harness side;
      variants `prompts/structured/`, `prompts/hints-only/`)
- [x] Placeholder rendering in `advisor-core` (lists in spec.md, Research design)
- [x] `prompts:` sets in config (directory or base + overrides), hashed by text, written into
      `advisor.json`; sweeps over `advisor.prompts` and `advisor.interventions`
- [x] `consult_requested` event; `prompt_hash` on `advisor_request`

## Week 7 — levels, stuck trigger, exposure scorers

- [x] Levels L0, L1, L3
- [x] Heuristic stuck trigger
- [x] Triggers `on_test_failure` and `periodic`; consult cooldown and advisor answer-token cap
- [ ] First real advisor item (Kimi K3) on one `dev` task: check briefs at each level by hand
- [x] Leakage and re-identification scorers (`leaked_units`, `role_map_leaks`, `--probe` with a
      memorisation floor)
- [ ] Run the probe on the pilot's briefs per level (top-1/top-3 against the floor)
- [ ] SWE-bench-Live import

## Before the pilot — devtest findings

- [x] Size limits as prompt targets + logged safety ceilings (brief in, advice out)
- [x] Anti-delegation: earned consults, required hypothesis, advice code cap, strictness per arm
- [x] Default prompts rewritten: the executor investigates and fixes, the advisor advises
- [x] "Plan after orientation" and "before done" triggers (`orient`, `before_done`)
- [ ] Revisit `experiments/pilot.yaml` for the new defaults: `max_answer_tokens: 800` is now
      below the 4000 safety ceiling, and `orient`/`before_done` are not in its interventions
- [ ] Smoke run with real models under the default (strict) rules: how often consults are refused
- [x] Scorers: rewards (resolve, partial, gold similarity, cost/exposure-penalised), dependence, leakage
- [ ] Pass the feedback scores to the prompt-search proposer (text, never the acceptance score)
- [ ] Calibrated LLM judge of advice quality (~30 hand-labelled consults first)

## Revised plan (2026-10-05) — before the pilot

See spec, [Revision 2026-10-05](spec.md#revision-2026-10-05-phase-consults-gating-brief-writer).
The pilot below waits for the go/no-go gate.

- [x] Offline uncertainty analysis, end-of-run signals (`scripts/uncertainty-signals.py`,
      `scripts/seed-agreement.py`; 145 A0 runs, current code)
- [ ] Gating signals and thresholds on run prefixes (what a gate sees mid-run)
- [ ] Context cost, before `--final`: ranged/capped reads or dedup of repeated reads, applied to all arms (A4 resends ~750k prompt tokens/run); decision log entry
- [x] run-tests repeats "build failed" at the end of its output (all arms; agent-side wrapper
      in the pi bundle, images unchanged)
- [ ] `L-best3`: three local attempts, local selection (build, tests, reproduction test)
      - [x] A0 at 9 seeds on the 25 gate tasks (`experiments/gate-a0.yaml`, 225 runs; 59 resolved)
      - [x] Local picker `bench pick`: build + existing tests per candidate patch, primary rule fixed
      - [x] `bench pick experiments/gate-a0.yaml` once gate-a0 is done (74 groups: local 25, oracle 38)
- [ ] Consult points `triage`, `plan` (critique of the executor's plan), `review` (candidate
      diffs); steer-don't-solve advisor prompt; uncertainty gating; value-of-consult and leak budget
      - [x] `review`, pick-only: `bench pick --review L3` (advisor-core `review.ts`, `prompts/review/`),
            gated on ≥ 2 distinct usable candidates
      - [x] `bench pick experiments/gate-a0.yaml --review L3` (≤ 75 consults; approved 2026-10-07,
            started early on complete groups): review 28 vs local 25; 1 group times out (fmt-3248 s1)
      - [ ] If review helps: its concerns to the executor for one revision run
      - [ ] `triage` (start consult + repo map), `plan` (orient's question as a plan critique)
      - [ ] Fold `abstractProse` into `buildBrief` at the next deliberate bundle change
- [ ] Gate: 25 signal tasks × 3 seeds, unredacted; go if ≥ 25% of the `L-best3`–A4 gap closed
      - [x] On `review` alone first (no new local runs): 7% of the gap, short (a perfect pick: 30%)
      - [x] Only if short: `experiments/gate-phase.yaml` (H-phase: `plan` + `orient` + `stuck`, L3,
            hints-only; existing triggers, no bundle change; 75 runs): 25/75, equal to `L-best3`'s 25 (0% of the gap;
            A0 at one run: 19.7 expected). No-go
      - [x] `experiments/gate-evidence.yaml` (H-evidence: evidence brief, restore rule; 75 runs):
            61 done, 19/60 paired vs `L-best3` 23 (−12% of the gap). No-go; 14 runs not run (quota)
      - [ ] Where the help is lost: advice uptake and advice correctness vs the upstream fix
- [ ] Optimization loop on 5 dev logic tasks × 2 seeds (`experiments/loop-case.yaml`), one change
      per iteration, then confirm on the held-out dev tasks
      - [x] Case protocol: `report_gate`, `case` prompt set (report, no code in the brief),
            failure slot holds only real build/test output
      - [x] Iteration 1: H-case (one report per run, never reopened): 4/10
      - [x] Empty-turn nudge (all arms)
      - [x] Iteration 2: H-case-close (closing report reviewed by Kimi): fmt-3248 0/2
      - [x] Iteration 3: H-case-tests (guidance: leave a test that encodes the bug failing):
            fmt-3248 2/2
      - [ ] H-case-tests on all 5 tasks; A0 + same guidance line as control
      - [x] Uptake measure per consult (`bench uptake`): iterations 1–3, right 16/20, right and
            fully followed 8/16
      - [x] Iteration 4: H-case-uptake (`experiment_report`, evidence read-back, acceptance
            check, recall is not evidence): 4/10; right and followed 18/23 (H-case 6/9)
      - [x] json-3564: why `test-element_access2` fails after a fix that follows right advice:
            an old `erase(first, last)` bug, hit once the suite runs over `ordered_json`
      - [x] Iteration 5: H-case-users (acceptance check from user code, existing tests to
            extend) on json-2225 and json-3564: 0/4; Kimi did not name the missing context
      - [x] Executor stalls: an empty-turn stop after 3 nudges ends a run with no patch
            (json-2225); an executor stuck in a sub-problem does not come back to consult:
            `come_back_turns`
      - [x] Iteration 6: H-case-back (come-back, stricter closing review) on json-2225 and
            simdjson-644: 3/4; Qwen did not consult right after a come-back
      - [x] H-case-back on all 5 loop tasks: 8/10 (H-case-uptake 4/10, A0 8/45)
      - [ ] H-case-back on held-out dev tasks (the tuning set is 5 of 25)
- [x] Interim write-up: `docs/results.md`, `docs/linkedin-draft.md` (2026-10-08)
- [ ] If go: brief writer with local retrieval, searched for resolve − λ × leaks; redaction
      levels none / surrogates / abstract
- [ ] If go: larger task pool (decision open), then `--final` on `test`
- [ ] Outputs: paper, public repo (README, one-command repro, secrets audit), MLflow write-up,
      LinkedIn post — all from the same figures
- Postponed: advice playbook (ACE), decoy briefs, provider splitting, advisor-side retrieval

## Week 8 — pilot

- [x] Pilot config `experiments/pilot.yaml`: 10 `dev` tasks (seeded stratified sample, gold
      patch ≤ 1000 lines), 3 seeds; A0, A4, and 3 prompt sets at L2 (150 items)
- [x] Pilot arms for the help-flow changes: `H-consult-only`, `H-clarify`, `H-memory`,
      `H-surrogates`; `orient_after: 3` explicit (now distinct files) — 300 items
- [x] Help-flow diagnostics: advice uptake (`uptake_rate`), answer length overshoot, skipped
      triggers by reason, `clarify` follow-up tokens; in `bench report` and MLflow
- [x] Planted-leak test for both leak detectors; role-aware rule, follow-ups scanned
- [x] Plugin implements `reserve_for_end`, `clarify`, `memory`, `surrogates`, `trigger_skipped`
      (contracts in 6a1ee51) before the pilot runs those arms
- [x] Trigger timing: `orient` by distinct files read; `stuck` reset by progress (test runs,
      new errors), reverts count, never after passing tests; `before_done` skipped after a
      passing run
- [x] Default prompts: concrete consult moments for the executor; word limit first and last
      and at most 3 next steps for the advisor; conditional prompt sections (`{{#name}}`)
- [ ] Smoke rerun (3 tasks, L2) to check self-consults, stuck timing and answer length
- [ ] Pilot on `dev`: 10 tasks, 3 seeds; arms A0, A4, and 3 help policies (prompt set ×
      initiative); variance estimate for the power analysis
- [ ] Gate A from the research proposal

## Weeks 9–10 — automatic prompt search

- [ ] `search:` config section; `train`/`val` split of `dev`, minibatches weighted toward tasks
      A0 fails and A4 solves
- [ ] `gepa` adapter over `Runner`: evaluate a candidate on a batch; reflective dataset from
      item directories (prompts, events, exit reason, grade log)
- [ ] `bench search`: resumable state in `runs/<experiment>/search/`, candidates with lineage in
      `prompts/search/`, `--export N` to named prompt sets
- [ ] Proposer calls through the metering proxy; search cost in the report
- [ ] Search runs on `dev` with the budget set from the pilot

## Week 11 — confirmation

- [ ] Choose the policies to confirm (before running anything on `test`)
- [ ] Run A0, A4, and the chosen policies once on `test`; paired comparisons; report the number
      of variants tried
