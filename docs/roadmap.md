# Roadmap

The build order from [spec.md](spec.md), at several days a week (~8 weeks to the pilot). Each
step ends with something that runs. Tick items as they land and note anything deferred.

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
- [ ] Multi-SWE-bench C++ importer
- [ ] arm64 image builds (strip x86-only flags)
- [ ] Gold-patch validation, run twice for flakiness
- [ ] Frozen manifest writer, with dropped instances and reasons
- [ ] First manifest; smoke task set (3 tasks)

## Week 3 — A0 end to end on smoke tasks

- [x] `runtime`: containers via Docker API (Colima or Docker Desktop), CPU and memory caps
- [x] `adapters`: protocol and `gold` adapter
- [ ] `adapters`: pi adapter and agent-layer image build
- [ ] Agent settings in config: `agents:` entries (adapter, version, options) with per-adapter
      option models; short form `agent: pi`; included in the config hash
- [ ] Metering proxy: one shared proxy, per-item routes, forced stream usage, `usage.jsonl` +
      `usage.schema.json`
- [ ] Usage preflight: refuse endpoints without `usage`; a call without usage fails the item
- [ ] Endpoint rewriting so agents only reach models through the proxy
- [x] `runner`: work-item expansion, parallel workers (`--parallel`), timeouts, retries
- [x] `grading`: apply patch and test patch in a fresh container, run `eval_command`
- [ ] `grading`: F2P and P2P test lists (with the importer)
- [ ] Measure the memory split between the local model and the VM
- [ ] Measure MLX throughput at `parallel` 1, 2, 4

## Week 4 — tracking and operations

- [x] MLflow tracking: runs per arm and item, artifacts, pinned inputs
- [ ] MLflow traces from the plugin
- [x] SQLite ledger and resume
- [ ] Debug mode, `bench shell`, `bench replay`
- [x] `bench report`: per-arm resolve rate with Wilson CI, exit reasons, time, turns; CSV
- [ ] Token budget (`limits.max_tokens`, exit reason `token_limit`)
- [ ] Tokens and cost in ledger, MLflow, and `bench report` (per arm, per resolved task);
      price table in the experiment
- [ ] Deterministic scorers (capability, cost)

## Weeks 5–6 — advisor plugin

- [ ] `advisor-core`: brief builder at L2, consult budget, advisor client, exposure log, event emitter
- [ ] `pi-binding`: consult tool, lifecycle hooks, advice injection, reading `advisor.json`
- [ ] Planning review and consult tool interventions
- [ ] OTel exporter to MLflow

## Week 7 — levels, stuck trigger, exposure scorers

- [ ] Levels L0, L1, L3
- [ ] Heuristic stuck trigger
- [ ] Leakage and re-identification scorers
- [ ] SWE-bench-Live import

## Week 8 — pilot

- [ ] Pilot: 10 tasks, arms A0, A2, A4, 3 seeds
- [ ] Gate A from the research proposal
