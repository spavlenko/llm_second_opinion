# llm_second_opinion — architecture spec

This is the source of truth for what we are building. It started as the Claude Docs page
[Architecture Spec: Advisor Evaluation Framework](https://claude.ai/code/artifact/374c3d0f-6cfc-445d-ba00-426cb2665cba)
(2026-09-27); this file supersedes it. Change this file when a decision changes, and log the
change in [Decision log](#decision-log). Progress is tracked in [roadmap.md](roadmap.md).

## Summary

llm_second_opinion is an open-source harness that runs coding agents on validated C++ tasks
and scores every run on capability, cost, and exposure. It has two parts: a Python harness
with MLflow for orchestration, grading, and tracking, and a TypeScript pi extension that
implements the advisor. Version 1 runs on a single Apple Silicon Mac.

**Goals**

- Compare experiment arms reproducibly on a frozen task set.
- Run unattended batches that resume after interruption, plus a debug mode for one task.
- Support other agents through adapters, with pi as the first.
- Record everything sent to the advisor, so exposure is measured, not assumed.
- Let others reuse it with their own tasks, models, and hardware.

**Non-goals for v1**

- Distributed or multi-machine execution.
- Windows or x86 hosts.
- A learned observer model for stuck detection.
- A custom UI beyond the MLflow UI and the CLI.

## Key decisions

| Area | Decision | Reason |
| --- | --- | --- |
| Name | `llm_second_opinion` (Python dist `llm-second-opinion`, npm scope `@llm-second-opinion`) | Chosen 2026-09-27 |
| License | MIT | Chosen 2026-09-27 |
| Audience | Open-source, reusable by others | The harness and task set are contributions in their own right |
| Harness | Python with MLflow 3 | Evaluation, tracing, and scoring ecosystem is in Python |
| Advisor | TypeScript extension inside pi | Simplest path; pi extensions are TypeScript |
| Agents | `AgentAdapter` interface, pi first | Other agents can be added without touching the runner |
| Configuration | YAML validated by Pydantic, Python API underneath | YAML for everyday use and sweeps; code for anything unusual |
| Modes | Resumable batch plus single-task debug, one code path | A bug seen in debug is the same bug that happens in batch |
| Platform | One Apple Silicon Mac, arm64 Linux containers | Matches the available hardware; no x86 emulation |
| Local model | Qwen3.8 via MLX behind an OpenAI-compatible endpoint | Any compatible server (llama.cpp, LM Studio, vLLM) also works |
| Advisor model | Kimi K3 via an OpenAI-compatible API | Provider is configuration, not code |
| Tasks | arm64-validated C++ subset, frozen manifest | Every arm sees an identical, verified task set |

**Trade-off: advisor inside the pi plugin.** Other agents run without advisor features until
the plugin is ported to them. To keep that port cheap, the plugin is split into
`advisor-core` (no pi imports) and `pi-binding` (pi-specific wiring). The config and event
contracts are versioned JSON Schemas, so the core could later move into a standalone service
without changing the harness.

## Architecture

The harness runs on the Mac host and starts one container per task and seed inside an arm64
Linux VM (Colima). The agent runs inside the container, so its edits, builds, and tests never
touch the host.

```
Mac host
├── harness (Python) ──────────── MLflow (runs, traces, artifacts)
│     │ starts / grades                ▲ spans + events
│     ▼                                │
│   Colima arm64 VM                    │
│     └── task container: pi + plugin ─┘
│              │ executor calls             │ abstracted briefs
├── local model (MLX, OpenAI API) ◄─┘       ▼
                                      advisor (Kimi K3, cloud)
```

The agent calls the local model on the host and sends abstracted briefs to the advisor; the
plugin streams spans and events to MLflow, and the harness logs grading results and scores
there too.

## Components

The harness owns everything outside the agent; the plugin owns everything the advisor does
inside a run.

**Python harness** (`harness/src/llm_second_opinion/`)

| Module | Responsibility |
| --- | --- |
| `contracts` | The three shared contracts as Pydantic models; JSON Schema export |
| `config` | Pydantic models for experiments, arms, models, limits, and task sets; YAML loading; a stable hash per arm config |
| `tasks` | Internal task format and frozen manifests. To come: dataset importers (Multi-SWE-bench, SWE-bench-Live), arm64 image builds, gold-patch validation |
| `runtime` | Container lifecycle through the Docker API (Colima or Docker Desktop), CPU and memory caps, exec with timeouts, file copy in and out |
| `adapters` | The `AgentAdapter` protocol; the `gold` adapter (applies the reference patch); the pi adapter, which builds an agent layer (Node, pi, plugin) on each task image, is to come |
| `runner` | Expands arms × tasks × seeds into work items; parallel workers, retries, and resume through the ledger |
| `ledger` | SQLite row per work item: status, attempts, grade, exit reason, turns, duration, MLflow run id |
| `grading` | Applies the agent's patch and the test patch in a fresh container and runs the task's eval command |
| `tracking` | MLflow: a run per arm with pinned inputs, a child run per item with metrics and artifacts; trace ingestion to come |
| `report` | Per-arm resolve rates with Wilson 95% intervals, exit reasons, time and turns; per-item CSV |
| `scorers` | Capability, cost, harm, identifier leakage, re-identification, and the calibrated advice judge |
| `mock_server` | OpenAI-compatible server replaying recorded completions (see Testing) |
| `cli` | The `bench` command |

**TypeScript plugin** (`plugin/`)

| Package | Responsibility |
| --- | --- |
| `advisor-core` | Trigger engine (planning review, consult tool, heuristic stuck detection); brief builder for L0–L3 with redaction and a local role map; consult budget; OpenAI-compatible advisor client; exposure log; event emitter. No pi imports. |
| `pi-binding` | Registers the consult tool, subscribes to pi lifecycle events, injects advice into the session, reads the run config |
| OTel exporter | Sends spans for turns, tool calls, triggers, and consults to MLflow |

## Contracts and interfaces

The harness and plugin never import each other; they share three versioned contracts. The
Pydantic models in `contracts.py` are the single source: `bench schemas` writes them to
`schemas/` as JSON Schemas, and TypeScript types are generated from those files. A test fails
if the committed schemas drift from the models.

1. **Run config (in)** — `run-config.schema.json`. The harness writes `advisor.json` into the
   container: run identity (experiment, arm, task, seed, config hash), executor model, advisor
   model, advisor settings (level, interventions, consult budget, stuck thresholds), and the
   events path. Secrets come from environment variables; the file only names the variable
   (`api_key_env`), never the key.
2. **Events (out)** — `event.schema.json`. The plugin appends to `events.jsonl` and emits
   matching OTel spans. The JSONL file is the source of truth for scoring; spans are for
   browsing in MLflow.
3. **Result (out)** — `result.schema.json`. The adapter returns the final `git diff`, the exit
   reason (`finished`, `turn_limit`, `time_limit`, `crash`), agent name and version, turns,
   duration, and error detail on a crash.

Every record carries `schema_version` (currently `"1"`). In the exported schemas every
property is required: producers always write every field (optionals as `null`), and the event
`type` discriminator must be mandatory for the generated TypeScript union.

**Agent adapter**

```python
class AgentAdapter(Protocol):
    name: str
    capabilities: frozenset[str]  # e.g. {"advisor", "otel"}

    def build_layer(self, task_image: str) -> str: ...
    def run(self, box: Container, task: Task, config: RunConfig, limits: Limits,
            env: dict[str, str]) -> AgentResult: ...
```

The runner writes `config` to `/run/advisor.json` before calling `run`, and passes API keys
in `env`; they are set per `exec`, never in the image or container config. One adapter
instance per arm is shared by parallel workers, so `run` keeps no state on the instance. The
runner refuses an arm that needs a capability the adapter lacks, such as an advisor arm on an
agent without the plugin. Adapters are registered by name in `adapters.ADAPTERS`; an arm's
`agent` picks one.

**Agent settings in config (planned).** An arm's `agent` becomes either a name (`pi`) or a
mapping, so the agent and its version are experiment variables like the models:

```yaml
agents:
  pi:     {adapter: pi, version: "0.x.y", options: {thinking: medium}}
  aider:  {adapter: aider, version: "0.x", options: {edit_format: diff}}
arms:
  - {name: A0, agent: pi, executor: local}
```

- `adapter` picks the registered `AgentAdapter`; `version` pins what is installed in the agent
  layer (and its image tag); `options` are adapter-specific and validated by the adapter's own
  Pydantic model (`options_model`), so a typo fails before anything runs.
- The whole agent entry is part of the config hash; changing the version or options gives new
  results.
- A short form `agent: pi` stays valid and means the adapter's defaults.

**Token metering (planned).** Token counts must not depend on each agent reporting them, so the
harness meters them itself: agents never call a model directly. One metering proxy per batch
(built on the mock server's upstream mode, threaded for parallel items) serves every item, and
the runner rewrites the endpoints
in `advisor.json` and the agent's model settings to point at it, one route per role
(`/items/<item>/executor/v1`, `/items/<item>/advisor/v1`). The proxy forwards to the real
endpoint and appends one record per call to `usage.jsonl`:

| Field | Meaning |
| --- | --- |
| `seq`, `ts`, `role` | Call order, time, and `executor` or `advisor` |
| `model` | Model id as sent |
| `prompt_tokens`, `completion_tokens`, `cached_tokens`, `reasoning_tokens` | From the provider's `usage` (streaming: `stream_options.include_usage` is forced on) |
| `latency_ms`, `status` | Wall time and HTTP status |

- **Only providers that report usage.** Counts come from the provider, never from estimates,
  so every endpoint must return `usage`, including in streams. Before a batch starts, the
  runner sends one tiny streaming request to each endpoint and refuses to run if the response
  has no `usage`. If a call during a run still arrives without it, the item fails (it is not
  recorded as done), so no estimated number enters the results.
- Usage is live: the runner can stop an item on a token budget (`limits.max_tokens`), recorded
  as a new exit reason `token_limit`.
- `usage.jsonl` becomes the fourth contract (`usage.schema.json`). `AgentResult` gains a
  `usage` summary per role; where an agent also reports its own counts, the report shows the
  difference as a sanity check.
- The ledger and MLflow get per-item totals (tokens per role, calls, cost from a price table in
  the experiment), and `bench report` shows tokens and cost per arm and per resolved task.
- The advisor plugin's `advisor_request`/`advisor_response` events stay the record of what was
  sent (exposure); the proxy is the record of what was used (cost). Both count the same advisor
  calls, which gives a cross-check.

**Event types.** Every event also has `schema_version`, `seq` (monotonic per run from 0),
`ts` (Unix seconds), and `type`.

| Event | Key fields |
| --- | --- |
| `trigger_fired` | intervention, reason, turn |
| `brief_built` | level, tokens, identifiers redacted, role-map size |
| `advisor_request` | request id, input tokens, brief text |
| `advisor_response` | request id, output tokens, cached tokens, latency (ms) |
| `advice_applied` | request id, turn it was injected at |
| `budget_exhausted` | consults used, limit |

Every `advisor_request` stores the exact text sent. The exposure scorers read only these
records, so what was measured is what actually left the machine.

## Experiment configuration

An experiment is one YAML file: the task manifest, seeds, limits, model endpoints, and a list
of arms. Each arm is validated against the Pydantic schema before anything runs. See
[`experiments/abstraction-sweep.yaml`](../experiments/abstraction-sweep.yaml).

- **Paths** are relative to the YAML file.
- **Environment variables**: `${VAR}` and `${VAR:-default}` in any string. All missing
  variables are reported at once. Inside `{...}` flow mappings the value must be quoted
  (`base_url: "${LOCAL_MODEL_URL}"`), or YAML fails to parse.
- **Seeds**: a count (`3` → seeds 0, 1, 2).
- **Arms** name an `executor` model, which must be a key in `models`. Advisor arms consult
  the model under the key `advisor`. Arm names must be unique.
- **Sweeps**: a list in `advisor.level` or `advisor.max_consults` expands into
  one arm per combination, named by suffix: arm `A2` with `level: [L1, L2]` becomes `A2-L1`
  and `A2-L2`. (`interventions` is a real list, not a sweep.)
- **Execution** (optional): `parallel` (work items at once, default 1), `cpus` and
  `memory_gb` (caps per container, default 4 and 8), `retries` (extra attempts after an
  infrastructure error, default 1), `grade_minutes` (default 30). Not part of the config hash.
  `bench run --parallel N` overrides `parallel`.
- **Config hash**: 16 hex chars of SHA-256 over the arm (minus its name), the limits, and the
  executor and advisor model settings (minus `base_url`). Renaming an arm or moving a server
  keeps results; changing behaviour invalidates them.

The same experiment can be built in Python for cases YAML does not cover:

```python
exp = Experiment.from_yaml("experiments/sweep.yaml")
exp.arms.append(exp.arm("A2").with_advisor(level="L3", name="A3"))
Runner(exp).run()
```

## Run lifecycle and modes

Every work item (arm, task, seed) goes through the same eight steps in batch and debug mode.

1. Load and validate the experiment; hash each arm's config.
2. Expand into work items and skip those already complete in the ledger with the same config hash.
3. Start a container from the task's agent-layer image with CPU and memory caps.
4. The adapter prepares the agent: writes `advisor.json`, sets endpoints and secrets.
5. The agent runs under the turn and time limits; the plugin streams events and spans.
6. The harness extracts the patch and grades it in a fresh container from the same task
   image, so the agent cannot alter the tests that grade it.
7. Results, cost, events, test logs, and the patch go to MLflow; the ledger marks the item done.
8. Scorers run over the stored records and can be re-run later without re-running agents.

Work items run on `execution.parallel` worker threads (default 1), in seed-major order so a
partial batch covers every arm and task evenly. One Mac serves one local model, so raise
`parallel` only when the model servers take concurrent requests (e.g. the cloud-only A4 arm,
or a local server with batching); container caps also have to fit the VM. An infrastructure
error (Docker, git, I/O) is retried `retries` times and then recorded as `failed` without
stopping the batch. On Ctrl-C, items in flight finish and queued ones are left for the next
run. An interrupted item restarts from step 3; there are no mid-run checkpoints.

Timeouts use `timeout` inside the container. It exits 124 (coreutils) or 143 (BusyBox), so a
command counts as timed out only if it also used the full time.

**CLI**

| Command | Purpose |
| --- | --- |
| `bench tasks build` | Import candidates and build arm64 task images |
| `bench tasks validate` | Run gold-patch validation and write the frozen manifest |
| `bench run EXP.yaml [--parallel N] [--arm A] [--task ID] [--mlflow URI] [--dry-run]` | Batch run; resumes where it stopped. MLflow logging is on when `--mlflow` or `MLFLOW_TRACKING_URI` is set |
| `bench run EXP.yaml --arm A2 --task ID --debug` | One task with live logs; keeps the container afterwards |
| `bench shell ITEM` | Open a shell in a kept container |
| `bench replay ITEM` | Step through a stored run's events: turns, triggers, briefs, advice |
| `bench score EXP.yaml` | Re-run scorers over stored runs |
| `bench report EXP.yaml [--csv F]` | Per-arm table from the ledger (stale config hashes ignored); per-item CSV. Plots to come |
| `bench schemas [--check]` | Regenerate (or verify) the contract JSON Schemas |
| `bench mock-server --recordings F [--upstream URL]` | Serve recorded completions; record from a real endpoint |

## Task pipeline

The pipeline turns benchmark instances into a frozen, arm64-validated task set; only tasks
that pass validation twice are kept.

1. **Import.** Pull C++ candidates from Multi-SWE-bench and SWE-bench-Live into one internal
   task format (repo, base commit, issue text, test patch, gold patch, test lists, creation date).
2. **Build.** Rebuild each environment for `linux/arm64`, removing x86-only compiler flags
   where present.
3. **Validate.** Apply the gold patch: fail-to-pass tests must fail before and pass after;
   pass-to-pass tests must pass both times. Run twice to catch flaky tests.
4. **Freeze.** Write a manifest with instance IDs, image digests, test lists, source, and
   creation date. Dropped instances are listed with the reason.
5. **Agent layer.** For each kept task, build a derived image with Node, pi, and the plugin,
   cached by plugin version.

**Internal task format** (`tasks.Task`): `id`, `image` (repo at the base commit), `workdir`
(default `/testbed`), `problem_statement`, `test_patch` (applied only at grading), `gold_patch`,
and `eval_command` (run in `workdir` at grading; exit 0 means resolved). The importers will
derive `eval_command` from the fail-to-pass and pass-to-pass test lists. A manifest is
`version` plus `tasks`; task ids must be unique.

The manifest is the only input experiments see. A new manifest version is a new task set,
and results from different versions are never mixed.

## Storage, resume, and reproducibility

A small SQLite ledger tracks what has run; MLflow stores what happened.

- **Ledger.** One row per work item, keyed by experiment, arm, task, seed, and config hash.
  It stores status, attempts, and the MLflow run and trace IDs. Changing an arm's config
  changes its hash, so stale results are never reused.
- **Item directory.** `runs/<experiment>/<arm>/<task>/seed-<n>/` holds `advisor.json`,
  `result.json`, `patch.diff`, `events.jsonl`, and `grade.log`, whether or not MLflow is on.
  The ledger is `runs/<experiment>/ledger.sqlite`.
- **MLflow layout.** One MLflow experiment per study; one parent run per arm (tagged with the
  config hash, reused on resume) holding the pinned inputs as params; one child run per task
  and seed with `resolved`, `turns`, `duration_s` and the item directory as artifacts. Traces
  per item come with the plugin's OTel exporter.
- **Pinned inputs.** Each run logs the manifest version, image digests, local model ID and
  MLX quantization, advisor model ID and reasoning effort, plugin and harness versions, and the seed.
- **Known non-determinism.** MLX sampling is not bit-exact across runs, and the advisor API
  can change behind the same model ID. Seeds reduce variance but do not remove it; the
  analysis relies on multiple seeds.
- **Secrets and network.** API keys live only in environment variables. The MLX server binds
  to the host interface the VM can reach, not to the wider network.

## Repository layout, packaging, and testing

```
llm_second_opinion/
  harness/          Python package (pip-installable), CLI `bench`
  plugin/
    advisor-core/   TypeScript, no pi imports
    pi-binding/     pi extension package
  schemas/          JSON Schemas: run config, events, results (generated)
  tasks/manifests/  frozen task sets
  experiments/      example YAML files
  docs/             this spec and the roadmap
  scripts/          dev helpers (local MLflow server)
```

- **Mock model server.** An OpenAI-compatible `/v1/chat/completions` (streaming and not)
  that replays recorded completions from JSONL in file order, single-threaded. With
  `--upstream`, requests past the end of the file are proxied to a real endpoint and
  appended to it (API key from `UPSTREAM_API_KEY`). Lets contributors and CI run end-to-end tests without a GPU or API keys.
- **Toy task set.** `tasks/toy/` (image) and `tasks/manifests/toy-v1.yaml`: two one-line
  shell bugs with test and gold patches. `experiments/toy.yaml` runs them with the `gold`
  agent, exercising containers, grading, the ledger, and the report with no model.
  Docker-backed tests (`test_docker.py`) build the image and skip when no daemon is running.
- **Smoke task set.** Three small validated tasks used in CI with the mock server, covering
  the full lifecycle including grading.
- **Tests.** Unit tests on both sides, plus contract tests that check the plugin's events
  against the schemas. Plugin tests are type-checked (`tsconfig.test.json`) before vitest runs.
- **Generated types.** `plugin/scripts/gen-types.mjs` merges `schemas/*.schema.json` into
  `advisor-core/src/contracts.ts` (committed); `pnpm check:types` fails on drift.
- **Local MLflow.** `scripts/mlflow-server.sh` runs a tracking server on 127.0.0.1:5050 with
  SQLite metadata and artifacts under `.mlflow/` (gitignored).

## Open questions


- [ ] Container runtime: Colima is the default because it exposes the Docker API the Python
      SDK expects. Revisit Apple's `container` tool if Docker API support is not needed.
- [ ] Memory split on 48 GB: how much for Qwen3.8 weights and KV cache versus the VM. Measure in week 3.
- [ ] Does pi's non-interactive mode expose everything the adapter needs (turn limit, clean
      exit reason), or does the adapter drive pi through its SDK?
- [ ] Where the brief builder's role map lives across a session, so advice maps back
      correctly after context compaction.
- [ ] Whether the re-identification attacker runs at scoring time only, or also as a live
      check that blocks a brief before sending.
- [ ] CLI name: keep `bench`, or rename (e.g. `lso`)?
- [ ] Safe `parallel` for the local model: measure MLX throughput at 1, 2, 4 concurrent sessions.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-09-27 | Project named `llm_second_opinion`; MIT license. |
| 2026-09-27 | `${VAR}` inside YAML flow mappings must be quoted; the original example did not parse. |
| 2026-09-27 | Config hash excludes arm name and model `base_url`. |
| 2026-09-27 | Exported schemas mark every property required; API keys referenced by env var name only. |
| 2026-09-27 | Sweepable fields limited to `advisor.level` and `advisor.max_consults`; sweep arms named `<arm>-<value>…`. |
| 2026-09-27 | Mock server skips `socket.getfqdn` on bind (it stalled 35 s on macOS). |
| 2026-09-27 | Dev toolchain: Node 26, pnpm 12, TypeScript 7, vitest 5, MLflow 3.16. |
| 2026-09-27 | Local MLflow on port 5050 (5000 is macOS AirPlay), SQLite store in `.mlflow/`. |
| 2026-09-27 | Generated TS types live in one file, `advisor-core/src/contracts.ts`, committed. |
| 2026-09-27 | Simplification pass: mock server replays in order only (no request-key matching, no `/v1/models`); seeds are a count; advisor model is always the `advisor` key; placeholder modules and CLI commands removed until built. |
| 2026-09-27 | Runs execute in parallel (`execution.parallel`, `--parallel`), default 1; supersedes "runs execute serially". |
| 2026-09-27 | Task format is `eval_command` + `test_patch` until the importers derive them from F2P/P2P lists. |
| 2026-09-27 | Runner writes `advisor.json`; adapters get API keys per exec. Adapter `prepare` folded into `run`. |
| 2026-09-27 | `gold` adapter and toy task set for harness tests without a model. Docker is a core dependency. |
| 2026-09-27 | MLflow: child run per item until OTel traces exist; reports use Wilson 95% intervals. |
| 2026-09-27 | Planned: agents configured as `agents:` entries (adapter, version, options) in the config hash; tokens metered by a harness proxy per item into `usage.jsonl`, not by the agents. |
| 2026-09-27 | One shared metering proxy per batch. Only endpoints that report `usage` (also when streaming) are allowed: preflight check before a batch; a call without usage fails the item. No tokenizer estimates. |
