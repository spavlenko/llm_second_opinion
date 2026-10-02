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
- [ ] Recipe for yhirose/cpp-httplib, then import `full`

## Week 3 — A0 end to end on smoke tasks

- [x] `runtime`: containers via Docker API (Colima or Docker Desktop), CPU and memory caps
- [x] `adapters`: protocol and `gold` adapter
- [x] `adapters`: pi adapter and agent bundle (volume); turn limit via a pi extension
- [x] Agent settings in config: `agents:` entries (adapter, version, options) with per-adapter
      option models; short form `agent: pi`; included in the config hash
- [ ] Executor prompt as a prompt slot (`executor_guidance`), replacing the adapter's fixed prompt
- [ ] Metering proxy: one shared proxy, per-item routes, forced stream usage, `usage.jsonl` +
      `usage.schema.json`
- [ ] Usage preflight: refuse endpoints without `usage`; a call without usage fails the item
- [ ] Endpoint rewriting so agents only reach models through the proxy
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
- [ ] Advisor spans (triggers, briefs, consults) in the item traces
- [x] SQLite ledger and resume
- [ ] Debug mode, `bench shell`, `bench replay`
- [x] `bench report`: per-arm resolve rate with Wilson CI, exit reasons, time, turns; CSV
- [ ] Token budget (`limits.max_tokens`, exit reason `token_limit`)
- [ ] Tokens and cost in ledger, MLflow, and `bench report` (per arm, per resolved task);
      price table in the experiment
- [ ] Deterministic scorers (capability, cost)
- [ ] Paired comparisons in `bench report`: differences with paired bootstrap CI, McNemar;
      lift over A0 and share of the A0–A4 gap closed; Pareto front of resolve rate vs cost

## Weeks 5–6 — advisor plugin

- [ ] `advisor-core`: brief builder at L2, consult budget, advisor client, exposure log, event emitter
- [ ] `pi-binding`: consult tool, lifecycle hooks, advice injection, reading `advisor.json`
- [ ] Planning review and consult tool interventions
- [ ] OTel exporter to MLflow
- [ ] Prompt slots (`executor_guidance`, `consult_tool`, `brief`, `advisor_system`,
      `advice_injection`) with `prompts/default/`; placeholder validation
- [ ] `prompts:` sets in config (directory or base + overrides), hashed by text, written into
      `advisor.json`; sweeps over `advisor.prompts` and `advisor.interventions`
- [ ] `consult_requested` event; `prompt_hash` on `advisor_request`

## Week 7 — levels, stuck trigger, exposure scorers

- [ ] Levels L0, L1, L3
- [ ] Heuristic stuck trigger
- [ ] Triggers `on_test_failure` and `periodic`; consult cooldown and advisor answer-token cap
- [ ] Leakage and re-identification scorers
- [ ] SWE-bench-Live import

## Week 8 — pilot

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
