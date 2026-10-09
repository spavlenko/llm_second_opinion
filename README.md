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

- **Executor:** Qwen3.8 (27B), run locally on the experimenter's own hardware behind an
  OpenAI-compatible endpoint (any compatible server works). It does all the work: reading
  code, editing, building, running tests. Its traffic stays on trusted machines, so exposure
  counts only what reaches the advisor.
- **Advisor:** Kimi K3 (Kimi Code plan) through an OpenAI-compatible API. It never touches
  the repository; it only sees the briefs the executor sends it.
- **Agent:** [pi](https://github.com/earendil-works/pi), a coding agent, runs non-interactively
  in each task's container. The advisor is a pi extension (`plugin/`).
- **Tasks:** real bug fixes from [Multi-SWE-bench](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench_mini)
  (C++: nlohmann/json, fmt, simdjson, Catch2), rebuilt and validated for arm64. The current
  task set (`mswe-full-cpp-v1`) is 123 tasks, split into `dev` (60) and `test` (63); tuning
  uses 25 `dev` tasks with headroom (the local model alone is flaky on them).
- **One model pair.** The study fixes this executor–advisor pair and searches many prompt
  versions for it; whether the prompts transfer to other pairs is a follow-up.

### Arms

| Arm | Who does the work | Role |
| --- | --- | --- |
| **A0** | Executor alone | Floor: what the local model manages without help |
| **`L-best3`** | Executor alone, three runs; a local picker (build and existing tests) keeps one patch | The bar: free in cloud tokens and exposure |
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
  - **Initiative:** the executor asks through a consult tool; the harness triggers help at planning, after orientation, when the agent looks stuck, after a failed test run, every N turns, or before it finishes; or both.
  - **Content:** four abstraction levels, L0–L3, for how much code and how many identifiers leave the machine.
  - **Budget:** consults per task, answer length, cooldown between consults.
  - **Anti-delegation:** the executor does the work; consults must be earned (own work, a cooldown, a stated hypothesis) and code in advice is cut to a few lines. Strictness is set per arm.

### What is measured

- **Primary:** resolve rate, meaning the issue's fail-to-pass tests pass and nothing else breaks.
- **Secondary, per policy:**
  - advisor tokens and cost per resolved task
  - number of consults, and when the first one happens
  - advice uptake
  - exposure: tokens and identifiers sent
- **Relative to the arms:** lift over A0 on the same task and seed, and the share of the gap closed, **(H − `L-best3`) / (A4 − `L-best3`)**. A policy must close at least 25% to go on.
- **Which consults mattered:** advice uptake (an offline judge that sees the upstream fix: was the advice right, did the final patch follow it?) and replays: a logged run is replayed up to one consult, then the live executor goes on with the advice or with a neutral reply.

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

Interim results, on `dev` only (nothing has touched `test`); details in
[docs/results.md](docs/results.md), run-by-run findings in [docs/lab-notes.md](docs/lab-notes.md).

| Arm (25 `dev` tasks) | Resolved |
| --- | --- |
| A0, one run | 26% |
| `L-best3` | 25 / 74 groups |
| A4, Kimi as the agent | 93% |

- **Hints did not pass the gate.** Up to 3 hint consults per run (`H-phase`, `H-evidence`)
  help a single run but not the `L-best3` bar; Kimi as a patch reviewer closes 7% of the gap.
- **Gates work where hints do not.** The case protocol (`H-case-back`) makes Qwen investigate
  and file a report before its first edit, run an experiment before editing, and pass a
  closing review before it stops: 8 / 10 on the 5 tuning tasks. Qwen obeyed every gate that
  refused a tool call and declined every optional hint.
- **Qwen stops; it does not get lost.** 91% of failed runs end with Qwen declaring the task
  done. Build failures on "interface" tasks are hidden tests calling names the issue never
  gives; no advice helps there.
- **Running now:** `H-case-back` on the 20 held-out `dev` tasks, and replays of the tuning
  runs that ask which consults decide the outcome (so far: the closing consult never did).

Built and tested:
- **Task pipeline:** arm64 images from per-repository recipes, gold-patch validation, frozen
  manifests with a `dev`/`test` split.
- **Runner:** items in parallel in containers, resumable, retries infrastructure errors
  (endpoint outages are never scored as agent failures), grades each patch in a fresh
  container by per-test results.
- **pi adapter:** turn and time limits, a clean exit reason for every run, record/replay of a
  logged run through the mock model server (`replay` option).
- **Advisor plugin:** a pi extension with the consult tool, harness triggers and gates
  (report before the first edit, experiment before editing, closing review, come-back, stuck),
  memory across consults, briefs at levels L0–L3 with identifiers redacted and mapped back,
  every request logged exactly.
- **Token metering:** every model call goes through a harness proxy that records the
  provider's own token counts per item (`usage.jsonl`), adds the API keys on the host (agent
  containers hold no secrets), holds and retries rate limits, and enforces an optional budget.
- **MLflow tracking:** a run per arm, per task and seed, and a trace per item.
- **Analysis:** `bench report` (paired comparisons, bootstrap CIs, McNemar, gap closed),
  `bench pick` (`L-best3`), `bench uptake`, `bench consult-value`, `bench score`,
  `bench regrade`.
- **Mock model server:** CI and development run without a GPU or API keys.

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
- `scripts/` — MLflow server, grader calibration, smoke-run checker
- `docs/` — [spec](docs/spec.md) (source of truth for the design), [results](docs/results.md),
  [roadmap](docs/roadmap.md), [lab notes](docs/lab-notes.md),
  [related work](docs/related-work.md), [task pipeline notes](docs/task-pipeline.md)

v1 targets a single Apple Silicon Mac with arm64 Linux containers.

## Development

Requires Python 3.11+ (developed on 3.14), Node and pnpm (developed on Node 26, pnpm 12:
`brew install node pnpm`), and a Docker API (Docker Desktop or Colima) for running experiments.

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
pnpm check:types && pnpm build && pnpm test
```

After changing `harness/src/llm_second_opinion/contracts.py`, regenerate the schemas and the
plugin's types: `bench schemas`, then `pnpm gen:types` in `plugin/`.

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

### Real models

Endpoints and keys go in a gitignored `.env` at the repository root (`cp .env.example .env`);
`bench` loads it, and only variable names appear in experiment files. Give Docker about
24 GB. Every batch starts with a usage preflight, so a wrong URL or key, or an endpoint that
does not report token usage, fails before anything runs.

```sh
bench run experiments/smoke-toy.yaml --runs-dir /tmp/lso-smoke     # minutes, all levels + A4
python scripts/smoke-check.py /tmp/lso-smoke/smoke-toy             # prints nothing wrong when clean
bench run experiments/calib-floor.yaml                              # A0/A4 headroom on dev
bench run experiments/gate-a0.yaml --parallel 2                     # A0 on the gate tasks
bench pick experiments/gate-a0.yaml --csv runs/gate-a0-best3.csv    # L-best3 from those runs
bench run experiments/holdout-case.yaml --parallel 2                # H-case-back, held-out dev
bench uptake experiments/holdout-case.yaml                          # was the advice right, followed?
bench consult-value experiments/holdout-case.yaml                   # per consult: trigger, cost, outcome
bench run experiments/replay-loop.yaml --parallel 2                 # replay to a consult, then live
```

Tuning and smoke runs use `dev` only; `test` needs `--final` and is run once.

Results land in `runs/<experiment>/`: `ledger.sqlite` and one directory per attempt,
`<arm>/<task>/seed-<n>/<config_hash>/attempt-<k>/`, with `item.json` (what ran, from which
commit), `advisor.json`, `result.json`, `patch.diff`, `events.jsonl`, `grade.json` and
`grade.log`, `metrics.json`, `usage.jsonl` and `requests.jsonl` (one line per model call,
from the metering proxy), and the agent's own logs. `bench report` also writes
`report.json`; `bench regrade` grades stored patches again with the current grader.

Build the C++ task set (arm64 images, gold-patch validation, frozen manifest with a
`dev`/`test` split):

```sh
bench tasks import --dataset mini
bench tasks build mswe-mini-cpp --parallel 2
bench tasks validate mswe-mini-cpp --parallel 2
bench tasks freeze mswe-mini-cpp --version mswe-mini-cpp-v2 --out ../tasks/manifests/mswe-mini-cpp-v2.yaml
bench tasks freeze mswe-full-cpp --version mswe-full-cpp-v1 \
  --out ../tasks/manifests/mswe-full-cpp-v1.yaml --keep-splits ../tasks/manifests/mswe-mini-cpp-v2.yaml
```

## License

[MIT](LICENSE)
