# llm_second_opinion

Can a small local coding model get most of the way to a frontier model by asking one for a
**second opinion**, only when it needs it, and while sending as little of your code as possible?

llm_second_opinion is an open-source harness for answering that question on real C++ bug
fixes. A local model does the work inside a container; a cloud advisor can be consulted
through abstracted briefs. Every run is scored on **capability** (was the issue resolved?),
**cost** (advisor tokens and money), and **exposure** (what left the machine). Everything
sent to the advisor is logged, so exposure is measured, not assumed.

## The research

### Question

What is the best **help policy**, meaning the way a local executor model asks a cloud advisor for
help? The goal is to find policies that close as much as possible of the gap between the local
model alone and the advisor alone, at the lowest advisor cost and exposure, with results that
hold on tasks not used to tune them.

### Setup

- **Executor:** Qwen3.8, run locally with MLX on an Apple Silicon Mac. It does all the work:
  reading code, editing, building, running tests.
- **Advisor:** Kimi K3 through an OpenAI-compatible API. It never touches the repository; it
  only sees the briefs the executor sends it.
- **Agent:** [pi](https://github.com/earendil-works/pi), a coding agent, runs non-interactively
  in each task's container. The advisor is a pi extension (`plugin/`).
- **Tasks:** real bug fixes from [Multi-SWE-bench](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench_mini)
  (C++: nlohmann/json, fmt, simdjson, Catch2), rebuilt and validated for arm64. The current
  task set is 49 tasks, split into `dev` (24) and `test` (25).
- **One model pair.** The study fixes this executor–advisor pair and searches many prompt
  versions for it; whether the prompts transfer to other pairs is a follow-up.

### Arms

| Arm | Who does the work | Role |
| --- | --- | --- |
| **A0** | Executor alone | Floor: what the local model manages without help |
| **H** (many variants) | Executor, with help from the advisor under a help policy | What we optimise |
| **A4** | Advisor model does the whole task | Ceiling: what full cloud delegation achieves |

### What a help policy is

A help policy has two parts, both set in the experiment config (not in code) and hashed with
the results:

- **Prompts: what is said.** Five prompt slots:
  - **Executor guidance:** when and how to ask for help.
  - **Consult tool:** what the executor thinks the tool is for.
  - **Brief template:** how a request is framed, from a free question to structured (goal, what was tried, current error, hypothesis).
  - **Advisor system prompt:** hint, plan, or code; how long.
  - **Advice injection:** how the answer re-enters the executor's context.
- **Approach: when, what, and how much.**
  - **Initiative:** the executor asks through a consult tool; the harness triggers help at planning, when the agent looks stuck, after a failed test run, or every N turns; or both.
  - **Content:** four abstraction levels, L0–L3, for how much code and how many identifiers leave the machine.
  - **Budget:** consults per task, answer length, cooldown between consults.

### What is measured

- **Primary:** resolve rate, meaning the issue's fail-to-pass tests pass and nothing else breaks.
- **Secondary, per policy:**
  - advisor tokens and cost per resolved task
  - number of consults, and when the first one happens
  - advice uptake
  - exposure: tokens and identifiers sent
- **Relative to the arms:** lift over A0 on the same task and seed, and the share of the A0–A4 gap closed, **(H − A0) / (A4 − A0)**.

The result is a **Pareto front** of resolve rate against advisor cost and exposure, not a single
winner.

### Prompt search

Hand-written prompt sets are the starting points. An automatic search in the style of
[GEPA](https://arxiv.org/abs/2507.19457) (reflective prompt evolution) then improves them:
- A proposer model reads the records of a few runs (prompts, triggers, briefs, advice, test results) and rewrites one prompt slot at a time.
- A new version is kept if it does better. Candidates on the Pareto front over validation tasks stay in the pool.

Every candidate is an ordinary prompt set, run and tracked like any other arm.

### Keeping results honest

Tuning prompts is optimisation, and it overfits the tasks it is tuned on. So:

- **Split once.** The task set is split into `dev` and `test` once, with a fixed seed and within each repository, and the split is recorded in the manifest.
- **Tune on `dev` only.** All prompt development and search use `dev`; the search splits it further into train and validation.
- **Test once.** The few policies to confirm are chosen before anything runs on `test`; they run on it once, and only `test` numbers are reported as results. `bench run` refuses `test` tasks without `--final`, which is recorded.
- **Count the tries.** Every variant tried is kept in the ledger and MLflow, so the number of variants tried is reported alongside the results.
- **Compare in pairs.** Arms are compared on the same tasks and seeds, with paired bootstrap intervals and McNemar's test.

### Validating the tasks

Tasks are rebuilt for arm64 from our own per-repository recipes, and a task is kept only if
the reference fix passes twice.
- **Test lists.** Fail-to-pass and pass-to-pass tests are re-derived from our own runs.
  Upstream's lists come from x86-64 runs, where one compile error fails the whole suite.
- **Grading.** Only real test results count: a test binary left over from an earlier build can't pass a broken patch.

[docs/task-pipeline.md](docs/task-pipeline.md) has the details and the problems found along the way.

## Status

Built and tested:
- **Task set:** 49 validated C++ tasks, with a `dev`/`test` split and a 3-task smoke subset.
- **Runner:**
  - runs items in parallel in containers and resumes after interruption
  - retries infrastructure errors
  - grades each patch in a fresh container by per-test results
- **pi adapter:** turn and time limits, and a clean exit reason for every run.
- **MLflow tracking:** on for every run.
  - a run per arm with its pinned inputs and summary metrics
  - a run per task and seed
  - a trace per item: turns, model calls, tool calls, grading
- **Help policies in config:** prompt sets (five slots, hashed by text) with a baseline in
  `prompts/default/`, sweeps over prompt sets and interventions.
- **Report:** per-arm resolve rates, paired comparisons with A0 (bootstrap CI, McNemar, share
  of the A0–A4 gap closed), the number of variants tried.
- **Mock model server:** CI and development run without a GPU or API keys.

Next:
- token metering through a proxy
- the local model setup and the A0/A4 baselines
- then the advisor plugin, help policies, and prompt search

Progress is tracked in [docs/roadmap.md](docs/roadmap.md).

## Repository layout

- `harness/` — Python package and the `bench` CLI: orchestration, task pipeline, grading,
  MLflow tracking
- `plugin/advisor-core/` — TypeScript advisor logic, no pi imports
- `plugin/pi-binding/` — pi extension wiring
- `agents/pi/` — the pi bundle (Node, pinned pi, harness extensions) mounted into task containers
- `schemas/` — versioned JSON Schemas: run config, events, results
- `tasks/manifests/` — frozen task sets
- `tasks/repos/` — per-repository image recipes for the task pipeline
- `experiments/` — example experiment YAML files
- `docs/` — [spec](docs/spec.md) (source of truth for the design), [roadmap](docs/roadmap.md),
  [task pipeline notes](docs/task-pipeline.md)

v1 targets a single Apple Silicon Mac with arm64 Linux containers.

## Development

Requires Python 3.11+, Node 22+ and pnpm (`brew install node pnpm`), and a Docker API
(Colima or Docker Desktop) for running experiments.

```sh
cd harness
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
bench --help
```

```sh
cd plugin
pnpm install
pnpm build && pnpm test
```

Every experiment run is tracked in MLflow; `bench run` refuses to start without the server:

```sh
scripts/mlflow-server.sh   # MLflow UI and tracking at http://127.0.0.1:5050
```

Run the toy experiment end to end (containers, grading, ledger, report; no model needed):

```sh
docker build -t llm-second-opinion/toy:1 tasks/toy
bench run experiments/toy.yaml --parallel 4        # rerun to resume; --dry-run to list items
bench report experiments/toy.yaml --csv runs/toy.csv
```

The same with the pi agent against the mock model server (recorded replies fix `toy-add`; the
first run builds the pi bundle):

```sh
bench mock-server --recordings harness/tests/fixtures/pi-toy-add.jsonl --host 0.0.0.0 &
bench run experiments/toy-pi.yaml --task toy-add
```

Results land in `runs/<experiment>/`: `ledger.sqlite` and one directory per arm, task, and
seed with `advisor.json`, `result.json`, `patch.diff`, `events.jsonl`, `grade.log`, and the
agent's own logs.

Build the C++ task set (arm64 images, gold-patch validation, frozen manifest with a
`dev`/`test` split):

```sh
bench tasks import --dataset mini
bench tasks build mswe-mini-cpp --parallel 2
bench tasks validate mswe-mini-cpp --parallel 2
bench tasks freeze mswe-mini-cpp --version mswe-mini-cpp-v2 --out ../tasks/manifests/mswe-mini-cpp-v2.yaml
```

## License

[MIT](LICENSE)
