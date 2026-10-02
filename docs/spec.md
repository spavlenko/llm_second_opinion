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

What the experiments optimise is the **help policy**: the prompts and the approach by which a
local executor model asks a cloud advisor for help (see
[Research design](#research-design-help-policies)).

**Goals**

- Find help policies that raise the local executor's resolve rate per unit of advisor cost
  and exposure, with results that hold on tasks not used to tune them.
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
| Local model | Qwen3.8 (27B) behind an OpenAI-compatible endpoint, set in `.env` | Served from the user's own second machine; trusted, so its traffic is not exposure. Any compatible server works |
| Advisor model | Kimi K3 via an OpenAI-compatible API | Provider is configuration, not code |
| Tasks | arm64-validated C++ subset, frozen manifest | Every arm sees an identical, verified task set |
| Research variable | The help policy: prompt sets and help-seeking approach, both in config | Prompts and triggers can be iterated without code changes and are hashed with the results |
| Validity | Tune on a `dev` split, report on a held-out `test` split; paired comparisons | Iterating on prompts is optimisation and overfits the tasks it is tuned on |

**Trade-off: advisor inside the pi plugin.** Other agents run without advisor features until
the plugin is ported to them. To keep that port cheap, the plugin is split into
`advisor-core` (no pi imports) and `pi-binding` (pi-specific wiring). The config and event
contracts are versioned JSON Schemas, so the core could later move into a standalone service
without changing the harness.

## Research design: help policies

A **help policy** is everything that decides how the executor gets help from the advisor. It
has two parts, prompts and approach, and both are experiment variables set in config, not in
code. (Status: prompt sets, sweeps, and paired comparisons are built in the harness, and the
plugin renders and uses the prompts; see [The advisor plugin](#the-advisor-plugin).)

**Prompts: what is said.** Five prompt slots, each a text file under `prompts/`:

| Slot | Where it goes | What it controls |
| --- | --- | --- |
| `executor_guidance` | Appended to the executor's system prompt | When and how to ask for help (e.g. "after two failed builds", "before a large refactor") |
| `consult_tool` | The consult tool's description and argument descriptions | What the executor believes the tool is for and what it should put in a request |
| `brief` | The brief builder's template | How a request is framed: a free question, or structured (goal, what was tried, current error, hypothesis) |
| `advisor_system` | The advisor's system prompt | Form of the answer: hint, plan, or code; length; whether to name files |
| `advice_injection` | How advice re-enters the executor's context | Framing and position of the advice (e.g. as a reviewer's note after the last tool result) |

`prompts/default/` holds one file per slot (`<slot>.md`) and is the hand-written baseline
policy every set starts from; `prompts/structured/brief.md` (goal, tried, current error,
hypothesis, question) and `prompts/hints-only/advisor_system.md` (hints and next steps, no
code) are the first variants. The executor's tool is named `consult`, with arguments
`question`, `tried`, and `hypothesis`.

**Placeholders.** Templates use `{{name}}` placeholders from a fixed list per slot (defined
once, in `harness/src/llm_second_opinion/prompts.py`). The harness checks them when the
experiment loads: any `{{...}}` not in the slot's list fails with the file and the name, as
does an empty slot or an `advice_injection` without `{{advice}}`. The plugin renders them;
`{{name}}` is written exactly so, with no spaces and no escaping. A section with nothing in it
is dropped when rendering, in every slot: a heading line (text ending in `:`, no placeholder)
followed by a line holding only a placeholder that renders empty, or a single line of such a
heading and the empty placeholder (`Hypothesis: {{hypothesis}}`), goes with one blank line next
to it. So a turn-0 `plan` brief has no empty "What they have tried:".

| Slot | Placeholders | Rendered as |
| --- | --- | --- |
| `executor_guidance` | `{{max_consults}}` | The arm's `max_consults` |
| `consult_tool` | `{{max_consults}}` | The arm's `max_consults` |
| `brief` | `{{task_summary}}` | The issue text, abstracted at the arm's level |
| | `{{tried}}` | The consult call's `tried`; for a harness trigger, the plugin's summary of recent actions |
| | `{{error}}` | The latest failing build or test output, trimmed and abstracted; empty if none |
| | `{{hypothesis}}` | The consult call's `hypothesis`; empty for a harness trigger |
| | `{{question}}` | The consult call's `question`; for a harness trigger, a fixed question for that trigger |
| | `{{code}}` | Code excerpts at the arm's level; empty at L0 and L1 |
| | `{{level}}` | `L0`–`L3` |
| | `{{trigger}}` | What started the consult: `consult` (the executor) or the intervention's name |
| `advisor_system` | `{{level}}` | `L0`–`L3` |
| | `{{max_answer_tokens}}` | The arm's `max_answer_tokens`, or `unlimited` |
| `advice_injection` | `{{advice}}` (required) | The advisor's answer, with abstracted names mapped back |
| | `{{consults_left}}` | Consults left in the budget after this one |

Slot texts are stored with line endings normalised to `\n` and trailing whitespace removed.

**Approach: when, what, and how much.**

- **Initiative:** who decides to ask. The executor (the consult tool), the harness (triggers:
  plan review at the start, the stuck heuristic, a failed test run, every N turns), or both.
  These are `interventions`, extended with `on_test_failure` and `periodic`.
- **Content:** the abstraction level L0–L3 (how much code and how many identifiers leave the
  machine) together with the `brief` prompt.
- **Budget:** `max_consults`, a cap on advisor answer tokens, and a cooldown in turns between
  consults.

### The advisor plugin

How the plugin (`plugin/`) carries out a policy. Turns are counted from 0 (the first model
call is in turn 0).

**Abstraction levels.** Each level adds to the one below. Redaction replaces an identifier with
a role placeholder (`<function_1>`, `<type_2>`, `<variable_3>`, `<macro_1>`, `<namespace_1>`,
`<file_1>`, `<identifier_1>`); the role map lives for the whole run, so the same name always gets
the same placeholder, and placeholders in the advice are mapped back before it reaches the
executor.

| Level | What the advisor sees |
| --- | --- |
| L0 | Natural language only. The issue and the executor's words with code blocks dropped (`[code omitted]`), inline code dropped unless it is a single name, and every identifier and file name redacted. No error text and no test names: `{{error}}` is a one-line category ("The build fails.", "2 test(s) fail.", "A command fails."). Recent actions in words ("ran the tests (failing)", "edited `<file_1>`"), no commands. |
| L1 | L0 plus error messages: the error lines of the latest failing output (up to 8; compiler errors, failing ctest lines with test names), identifiers redacted. |
| L2 | L1 plus code: the issue's code blocks, redacted; up to 3 excerpts of ±3 lines around the `file:line` locations in the error output (or the last edit when there are none), with every non-keyword name redacted; up to 20 error lines; shell commands in recent actions, redacted. |
| L3 | Everything verbatim: the issue as written, error output, excerpts with real paths, commands. |

Prose redaction catches code-like tokens: qualified names (`a::b`), calls (`f()`, `f(x)`),
`snake_case`, `camelCase`, `PascalCase` with two humps or more, `ALL_CAPS_WITH_UNDERSCORES`,
names quoted as compilers quote them (`'Serializer'`), absolute paths and source file names.
Ordinary words stay. Code redaction replaces every name that is not a C++ keyword or a
well-known standard name; names under `std::` are public and kept at every level; string
literals become `"…"` and comments `// …`. `brief_built.identifiers_redacted` counts the
placeholders in the brief and `role_map_size` the names mapped so far; `brief_built.role_map`
maps each placeholder that appears in this brief to the real identifier, for the leakage and
re-identification scorers. The map stays on the machine (`events.jsonl` is local; only the brief
text is sent). `tokens` is an estimate (4 characters a token), which is the exposure measure
(cost comes from the metering proxy).

**Triggers.** The executor's consult tool needs only budget. Harness triggers:

- `plan`: once, before the first model call. The question is a fixed "How should I approach
  this task?" over the issue text, so there is no executor plan to review yet.
- `on_test_failure`: a `bash` call that runs `/opt/lso/run-tests` and fails: non-zero exit, or
  (when piped through `tail`) ctest's "N tests failed out of" with N > 0, "The following tests
  FAILED", or run-tests' "build failed".
- `stuck`, any of: the same tool call (name and arguments) `repeat_calls` times in a row; the
  same error `same_error` times since the last consult (error lines compared with numbers and
  whitespace normalised); `no_diff_turns` turns in a row without a file edit (the edit and
  write tools, or an in-place shell edit such as `sed -i`, `git apply`, `patch`).
- `periodic`: at the end of every `periodic_every`-th turn.

Harness triggers are checked at the end of each turn, at most one per turn, in the order
`on_test_failure`, `stuck`, `periodic`. Every consult resets the stuck counters. A harness
trigger may not fire in the turn of a consult nor in the `cooldown_turns` turns after it.
`max_consults` covers every consult, the executor's and the harness's, and counts attempts
(an advisor error uses one up); a consult refused for budget emits `budget_exhausted` the
first time only. `max_answer_tokens` is sent as the advisor request's `max_tokens`, and the
default `advisor_system` states it ("Answer length limit, in tokens, with your reasoning
counted toward it: 400.", or "unlimited"), since reasoning models spend part of it before the
answer. An answer the provider cut there (`finish_reason` "length") is still injected, with
"[advice truncated: the advisor hit its answer length limit]" appended on its own line;
`advisor_response.advice_text` keeps it as received.

**Where advice goes.** For the consult tool, the rendered `advice_injection` is the tool
result. For harness triggers it is a custom message (rendered by pi as a user message): the
plan review's is added by the `before_agent_start` handler, next to the prompt; the others are
appended at the turn's end (`turn_end` boundary entry with `continue: true`, so the model sees
it in its next request even when the turn would have ended the run). `executor_guidance` is
appended to pi's system prompt from `before_agent_start` (`appendSystemPrompt`), so no CLI
flag is needed. The task text for briefs is the issue inside the adapter's prompt
(`<issue>…</issue>`).

**What is logged.** Once at session start, before any trigger, `policy_rendered` records the
help-policy text the executor sees: the prompt hash, the rendered `executor_guidance` exactly as
appended to the system prompt, and the rendered consult tool description (null when the tool is
not registered). Every consult then emits, in order, `consult_requested` (the executor's
question) or `trigger_fired`, `brief_built`, `advisor_request` (before sending: the exact user
message and the prompt hash), then `advisor_response` or `advisor_error`, and `advice_applied`
when the advice was injected (with the turn it was injected in). `advisor_request.input_tokens`
is the 4-characters-a-token estimate; `advisor_response.prompt_tokens` and `reasoning_tokens`
are the provider's own counts from its `usage` (`prompt_tokens`,
`completion_tokens_details.reasoning_tokens`), null when it leaves them out. `advisor_error`
carries the HTTP status (null for a network error, timeout or abort) and the time spent. The
system message is the `advisor_system` slot, which holds no task data. Every advisor call sends
the header `X-LSO-Request-Id: <request_id>`, which the metering proxy records to join usage to
events. The endpoint's `temperature`, `top_p` and `sampling_seed` are sent (as `temperature`,
`top_p`, `seed`) when set. The plugin also writes `advice.jsonl` (`LSO_ADVICE_LOG`): per request,
the system and user messages, the advice as received, the text injected, and the whole role map
so far (placeholder to identifier), for reading runs and for prompt search; it is not a
contract. An advisor failure (HTTP error, the proxy's `token_limit` refusal, timeout after 5
minutes) is an `advisor_error`, and the executor gets "The advisor could not be reached" as the
tool result; it never stops the agent.

**Config.** Prompt sets are named once per experiment and chosen per arm. A set is a
directory, or an earlier set with some slots replaced. `advisor.prompts` and
`advisor.interventions` sweep like `level` (for `interventions`, a list of lists sweeps; a flat
list is one value):

```yaml
prompts:                                             # paths relative to this file
  structured: {base: default, brief: ../prompts/structured/brief.md}
  hints-only: {base: default, advisor_system: ../prompts/hints-only/advisor_system.md}

arms:
  - {name: A0, agent: pi, executor: local}          # floor: no advisor
  - name: H
    agent: pi
    executor: local
    advisor:
      prompts: [default, structured, hints-only]     # sweep: H-default-consult, ...
      interventions: [[consult], [consult, stuck], [plan, consult, stuck]]
      level: L2
      max_consults: 5
  - {name: A4, agent: pi, executor: advisor}        # ceiling: the advisor does the task
```

- A set is a directory with all five `<slot>.md` files (any other `.md` file there is an
  error), or `{base: <set>, <slot>: <file>, ...}`, which replaces those slots of another set.
  Chains are allowed; cycles and unknown names fail at load. `default` is `prompts/default/`
  at the repository root unless the experiment defines `default`. Every defined set, and
  every set an arm names, is read and checked when the experiment loads.
- An advisor arm's `advisor.prompts` names a set (default `default`). The config hash covers
  the set's **text** through `PromptSet.hash` (16 hex chars of SHA-256 over the five slot
  texts), not the file paths or the set's name: editing a prompt gives new results; moving a
  file or renaming a set does not. Arms without an advisor have no prompt set, and their
  hash is unchanged.
- The harness resolves the set and writes it into `advisor.json` as `prompts` (name, hash,
  and the five texts with placeholders unrendered), so the plugin needs no file access and
  each item directory records exactly which prompts were used. The arm's MLflow run records
  the set's name and hash.
- Sweep arms are named by suffix in the order the keys appear: a set's name for `prompts`,
  and the interventions joined by `+` for `interventions` (`none` for an empty list), e.g.
  `H-structured-consult+stuck`.

**What is measured.** Primary: resolve rate. Secondary, per policy:

- advisor tokens and cost per resolved task (from the metering proxy);
- consults per item, and time (turn) to the first consult;
- advice uptake (`advice_applied` per `advisor_response`);
- exposure: tokens and identifiers sent (from `advisor_request`);
- lift over A0 on the same task and seed, and the share of the A0–A4 gap closed,
  (H − A0) / (A4 − A0).

A good policy is on the Pareto front of resolve rate against advisor cost and exposure; the
report shows that front rather than a single winner.

**Keeping results honest.** Iterating on prompts is optimisation, and it overfits the tasks it
is tuned on.

- The frozen manifest is split once into `dev` and `test` (fixed seed, stratified by repository,
  recorded in the manifest).
- Prompt development and sweeps use `dev` only. The few policies to confirm are chosen before
  anything runs on `test`, run on it once, and only `test` numbers are reported as results.
- `bench run` refuses to run any task in the manifest's `test` split (an experiment with
  `split: test`, or with no split on a manifest that has one) unless given `--final`. The
  ledger records every batch with its split, its number of test tasks, and whether it was
  final; MLflow tags the arm and item runs of a final batch `lso.final=true`; `bench report`
  says how many batches were final, and warns about test tasks run without `--final`.
- Every variant tried on `dev` stays in the ledger and MLflow, so the number of variants tried
  is reported with the results: `bench report` prints the arms in the config and the number
  of distinct config hashes in the ledger, stale ones included (each prompt edit is a new
  hash).
- Arms are compared in pairs on the same tasks and seeds, which needs far fewer runs than
  comparing each arm's own interval. See [Paired comparisons](#paired-comparisons).

### Paired comparisons

`bench report` compares every arm with a baseline arm (`--baseline`, default `A0` when the
experiment has one) on the (task, seed) items both completed at their current config hash:

- **Difference** in resolve rate on those items, with a 95% paired bootstrap interval over
  tasks: tasks are resampled with replacement and a task's seeds stay together (seeds of one
  task are not independent); 10,000 resamples, fixed RNG seed 0, percentile interval.
- **McNemar's exact test** (two-sided binomial test) on the discordant items: resolved by the
  arm only, and by the baseline only. It treats items as independent, so with several seeds
  per task the bootstrap interval is the primary measure.
- **Lift over A0**: the difference itself (percentage points) and relative to the
  baseline's rate.
- **Share of the gap closed**, (H − A0) / (A4 − A0), with a ceiling arm (`--ceiling`, default
  `A4` when present), on the items all three completed, with a bootstrap interval from the
  same resampling. Undefined when the ceiling does not beat the baseline; resamples where it
  does not are skipped, and no interval is given if fewer than half remain.
- **Pareto front** of resolve rate against advisor cost per item, over all done items: cost
  is the ledger's `cost_usd`, or else advisor prompt + completion tokens (for an arm whose
  executor is the advisor model, its executor tokens as well). Arms that never call the
  advisor model cost 0; an arm with an item lacking cost data has no cost and is left off
  the front. Without those ledger columns the report says "no cost data".

`--pairs-csv F` writes one row per compared arm. After each batch, the arm runs in MLflow get
the same numbers as metrics against `A0` (`paired_items`, `paired_diff` and its interval,
`paired_mcnemar_p`, `paired_rel_lift`, `gap_closed` and its interval). Only the standard
library is used.

**One model pair.** The study fixes one executor–advisor pair (Qwen3.8 via MLX and Kimi K3)
and searches many prompt versions for it. Prompts are tuned to this pair; whether they
transfer to other pairs is a follow-up check, not a goal of v1.

### Automatic prompt search

Hand-written prompt sets are the starting points; an automatic search then proposes new
versions and keeps the ones that do better. The method is reflective evolution in the style of
GEPA: a proposer model reads what happened in a few runs and rewrites one prompt slot at a
time.

1. **Start** from a prompt set (e.g. `baseline`) and evaluate it on a validation subset of `dev`.
2. **Pick** a candidate from the pool (a Pareto front over validation tasks, so a candidate
   that is best on some tasks survives even if its average is not the highest) and a slot to
   change.
3. **Run** the candidate on a small minibatch of training tasks and collect each run's record:
   the prompts, the events (triggers, briefs, advice, uptake), the exit reason, and the
   grading log.
4. **Reflect:** the proposer model gets those records and the slot's current text, and writes a
   new version of that slot.
5. **Accept** the new candidate if it does better on the same minibatch; then evaluate it on
   the validation subset and add it to the pool.
6. Repeat until the rollout budget is spent. Every candidate is an ordinary prompt set, so it
   runs through the same runner, ledger, grading, and MLflow as any arm.

The search is built on the `gepa` package: its adapter interface (evaluate a candidate on a
batch; turn traces into a reflective dataset) maps onto `Runner` and the item directories, and
a candidate is exactly a mapping of slot names to texts. If the package does not fit, the
same loop is small enough to own.

**Splits.** `test` is never seen by the search. The search splits `dev` further into `train`
(minibatches for reflection) and `val` (acceptance and the Pareto pool). Tasks that A0 fails
and A4 solves carry the most signal, so minibatches favour them; tasks every arm solves or
every arm fails tell the search nothing.

**Config.**

```yaml
models:
  local:    {base_url: "${LOCAL_MODEL_URL}", model: qwen3.8}
  advisor:  {base_url: "${ADVISOR_URL}", model: kimi-k3, api_key_env: ADVISOR_API_KEY}
  proposer: {base_url: "${ADVISOR_URL}", model: kimi-k3, reasoning_effort: high, api_key_env: ADVISOR_API_KEY}

search:
  arm: H                   # arm whose prompts are searched; its other settings stay fixed
  start: [baseline, structured]
  slots: [executor_guidance, consult_tool, brief, advisor_system, advice_injection]
  proposer: proposer       # key in models
  val_tasks: 8             # taken from dev; the rest of dev is train
  minibatch: 4
  budget: {rollouts: 300}  # agent runs, the real cost
  seed: 0
```

- `bench search EXP.yaml` runs the loop and resumes after interruption; its state (pool,
  lineage, scores) lives in `runs/<experiment>/search/`.
- Each candidate is written to `prompts/search/<experiment>/<candidate>/`, one file per slot,
  with its parent, the slot changed, and the proposer's reasoning. `bench search --export N`
  turns the top N into named prompt sets for the confirmation run on `test`.
- Proposer calls go through the metering proxy, so search cost is reported with the results.
  Proposer inputs contain task code and logs from public benchmark repositories; they are
  logged like advisor requests.

**Cost.** One rollout is one agent run, up to `limits.wall_minutes`. At an average of 10
minutes and `parallel` 1, 300 rollouts take about 50 hours on the Mac, so the budget is set
from the pilot's measured run time, and approach settings (initiative, budget) are fixed or
swept by a small grid rather than searched together with the prompts.

**Contracts (defined 2026-10-02).** An arm's `advisor.prompts` is the *name* of a prompt set
(it sweeps like `level`); the harness resolves it into `RunConfig.prompts` (`name`, `hash`,
and `texts`, one per slot, placeholders unrendered). `AdvisorSettings` also gains
`max_answer_tokens`, `cooldown_turns`, and `periodic_every` (required with `periodic`);
interventions are `plan`, `consult`, `stuck`, `on_test_failure`, `periodic`. A new event
`consult_requested` records an executor-initiated request with the executor's stated reason
and the turn, and `advisor_error` a failed advisor call. `advisor_request` gains
`prompt_hash`, so every exposure record names the prompts that produced it.

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
| `tasks`, `pipeline` | Internal task format and frozen manifests; the Multi-SWE-bench importer, arm64 image builds from per-repo recipes, gold-patch validation, freezing with a `dev`/`test` split. SWE-bench-Live import is to come |
| `runtime` | Container lifecycle through the Docker API (Colima or Docker Desktop), CPU and memory caps, exec with timeouts, file copy in and out |
| `adapters` | The `AgentAdapter` protocol; the `gold` adapter (applies the reference patch); the pi adapter (pi in JSON mode, bundle mounted from a volume with the advisor plugin, turn limit by a harness extension) |
| `runner` | Expands arms × tasks × seeds into work items; parallel workers, retries, and resume through the ledger |
| `ledger` | SQLite row per work item: status, attempts, grade, exit reason, turns, duration, MLflow run id, tokens per role, model calls, cost |
| `metering` | The metering proxy (per-item routes, secrets added on the host, `usage.jsonl`, token budget), the usage preflight, and cost from the price table |
| `grading` | Applies the agent's patch and the test patch in a fresh container, runs the task's eval command, and checks its per-test results against the task's test lists (`testlogs` parses ctest output) |
| `tracking`, `tracing` | MLflow, mandatory for `bench run`: a run per arm with pinned inputs, git commit, and summary metrics; a child run per item with metrics, artifacts, and a trace built from the agent's logs |
| `report` | Per-arm resolve rates with Wilson 95% intervals, exit reasons, time and turns; per-item CSV |
| `scorers` | Capability, cost, harm, identifier leakage, re-identification, and the calibrated advice judge |
| `mock_server` | OpenAI-compatible server replaying recorded completions (see Testing) |
| `cli` | The `bench` command |

**TypeScript plugin** (`plugin/`)

| Package | Responsibility |
| --- | --- |
| `advisor-core` | `config` (reads and checks `advisor.json`), `template` (`{{name}}` rendering), `observe` (tool calls as agent-neutral observations; test runs, errors, edits), `triggers` (trigger engine, cooldown, consult budget), `redact` (role map, prose and code redaction), `brief` (L0–L3 brief builder), `client` (OpenAI-compatible advisor client), `session` (one run's advisor: observations in, consults out, every event emitted), `events` (`EventWriter`). No pi imports. |
| `pi-binding` | The pi extension: reads `advisor.json` (`LSO_ADVISOR_CONFIG`), registers the `consult` tool when `consult` is an intervention, maps pi's `before_agent_start`, `turn_start`, `tool_result`, and `turn_end` to the session, injects advice. `scripts/bundle-pi.mjs` (esbuild) bundles it with advisor-core into one file, `lso-advisor.js`; pi provides `typebox` and its own packages at run time |
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
   (`api_key_env`), never the key. For a metered agent the endpoints are the metering
   proxy's routes, with no key names or headers at all.
2. **Events (out)** — `event.schema.json`. The plugin appends to `events.jsonl` and emits
   matching OTel spans. The JSONL file is the source of truth for scoring; spans are for
   browsing in MLflow.
3. **Result (out)** — `result.schema.json`. The adapter returns the final `git diff`, the exit
   reason (`finished`, `turn_limit`, `time_limit`, `token_limit`, `crash`), agent name and
   version, turns, duration, error detail on a crash, and the metered `usage` per role (filled
   by the runner).

Every record carries `schema_version` (currently `"1"`). In the exported schemas every
property is required: producers always write every field (optionals as `null`), and the event
`type` discriminator must be mandatory for the generated TypeScript union.

**Agent adapter**

```python
class AgentAdapter(Protocol):
    name: str
    version: str
    capabilities: frozenset[str]  # e.g. {"advisor", "otel"}
    uses_models: bool             # if so, metered: calls go through the metering proxy
    artifacts: tuple[str, ...]    # container files copied into the item directory

    def __init__(self, spec: AgentSpec): ...            # the experiment's `agents` entry
    def build_layer(self, task_image: str) -> Layer: ...  # image + read-only volumes
    def run(self, box: Container, task: Task, config: RunConfig, limits: Limits,
            env: dict[str, str]) -> AgentResult: ...
```

The runner writes `config` to `/run/advisor.json` before calling `run`. An agent that uses
models gets the metering proxy's routes in `config` and an empty `env` (the proxy adds the
keys); any other gets the API keys in `env`, set per `exec`, never in the image or container
config. One adapter
instance per arm is shared by parallel workers, so `run` keeps no state on the instance. The
runner refuses an arm that needs a capability the adapter lacks, such as an advisor arm on an
agent without the plugin. Adapters are registered by name in `adapters.ADAPTERS`.

**pi adapter.** pi (`@earendil-works/pi-coding-agent`, default 0.99.1) runs non-interactively
in JSON mode (`pi --mode json`), which writes one event per line; the adapter keeps that
stream (`pi.jsonl`), the prompt, and stderr as item artifacts.

- *Bundle.* `agents/pi/Dockerfile` builds an image with Node 24 and the pinned pi under
  `/opt/lso-agent`; its contents are copied once into a Docker volume that every agent
  container mounts read-only there. Task images stay as validated: copying the ~560 MB
  bundle into each task image would store it once per task (Docker does not share those
  layers). Node runs from the bundle by absolute path and is not put on the agent's `PATH`.
  Official Node binaries need glibc 2.28, which all task images have (the oldest is Debian
  buster); the toy image is Debian slim for the same reason. A first build stage bundles the
  advisor plugin from its sources (passed as named build contexts `advisor-core`,
  `pi-binding`, `plugin-scripts`) into `extensions/lso-advisor.js`, so the bundle needs no
  network at run time and no Node on the host. The adapter names the volume after the image
  ID that its own build printed, not the tag's, since another checkout may retag.
- *Advisor.* Advisor arms (capability `advisor`) add `-e extensions/lso-advisor.js` and set
  `LSO_ADVISOR_CONFIG=/run/advisor.json`, `LSO_ADVICE_LOG=/run/lso/advice.jsonl` (an item
  artifact), and `LSO_ADVISOR_BASE_URL` (the advisor endpoint as the container reaches it).
  The plugin calls that URL as given and sends `Authorization` only if `api_key_env` names a
  set variable, plus `headers` and the set `header_env` values; under metering all three are
  empty and the proxy adds credentials. Its events go to `events_path`, which the runner
  copies out as `events.jsonl`.
- *Model.* The executor endpoint becomes pi's only provider (`lso`, OpenAI-compatible) in a
  `models.json` under `/run/lso/agent` (`PI_CODING_AGENT_DIR`), kept as an item artifact.
  pi is metered, so its base URL is the item's proxy route and it has no key (`apiKey:
  "none"`, which the proxy replaces). Host-local URLs would be rewritten to
  `host.docker.internal`.
- *Isolation.* `--no-extensions` (so no MCP or codemode), no skills, prompt templates, themes,
  or context files (`AGENTS.md`/`CLAUDE.md` in a task repository), `--offline`, no telemetry.
  Tools are pi's defaults (read, bash, edit, write) unless the options say otherwise.
- *Limits.* pi has no turn-limit flag, so the harness extension `agents/pi/limits.ts` counts
  turns, records the limit in `/run/lso/exit.json`, and aborts; the turn the abort cuts short
  is not counted. The wall-clock limit is `timeout` around `exec pi`.
- *Exit reason.* `time_limit` if the timeout fired; `turn_limit` if the extension recorded
  it; `crash` if pi exited non-zero or the last assistant message ended in an error (for
  example the model endpoint failing after pi's retries); otherwise `finished`. Turns are
  pi's `turn_end` events.
- *Prompt.* Until prompt slots exist, a fixed instruction with the issue text: fix the
  source, do not change existing tests, `/opt/lso/run-tests` rebuilds and runs the tests.
- *Options.* `thinking` (default: the endpoint's `reasoning_effort`), `tools`,
  `context_window`, `max_output_tokens`, `thinking_level_map` (pi level to the endpoint's
  `reasoning_effort` value), and `compat` (pi's endpoint compatibility flags, passed as is).
  The endpoint's `headers` and `header_env` become provider headers in `models.json`, secret
  ones as `${VAR}` references that pi resolves from the exec environment (both are empty
  under metering: the proxy adds them).

**Agent settings in config.** An arm's `agent` names an entry in `agents`, or an adapter
directly, so the agent and its version are experiment variables like the models:

```yaml
agents:
  pi:     {adapter: pi, version: "0.x.y", options: {thinking: medium}}
  aider:  {adapter: aider, version: "0.x", options: {edit_format: diff}}
arms:
  - {name: A0, agent: pi, executor: local}
```

- `adapter` picks the registered `AgentAdapter`; `version` pins what is installed in the
  agent's bundle; `options` are adapter-specific and validated by the adapter's own Pydantic
  model when the runner starts, so a typo fails before anything runs.
- The whole agent entry (not its name) is part of the config hash; changing the version or
  options gives new results.
- A short form `agent: pi` means the adapter's defaults.

**Token metering.** Token counts must not depend on each agent reporting them, so the harness
meters them itself (`metering.py`): agents never call a model directly. One metering proxy per
batch (a threaded HTTP server; the mock server's upstream mode buffers whole responses, so it
was not reused) serves every item of an agent that calls models (`AgentAdapter.uses_models`;
not `gold`). For each item attempt the runner registers a random item id, and rewrites the
endpoints in `advisor.json` and so in the agent's model settings (pi's `models.json`) to
`http://host.docker.internal:<port>/items/<id>/<role>/v1`, with `api_key_env`, `headers`, and
`header_env` cleared. The config hash is unchanged.

- **Secrets stay on the host.** The proxy adds `Authorization: Bearer <api_key_env>`,
  `headers`, and `header_env` values from the host environment; a client's own
  `Authorization` is dropped. Containers get the proxy URL and no keys (the per-exec `env` is
  empty for metered agents), and the real endpoints never appear in the item directory.
- **Binding.** The proxy binds 127.0.0.1 (`bench run --proxy-host` to change). Docker Desktop
  for Mac forwards the containers' `host.docker.internal` (host gateway) to the Mac's loopback
  (checked with the toy image and the pi Docker tests), so nothing else on the network can
  use the proxy. A VM that does not do this (e.g. Colima) needs an address it can reach.
- **Streaming.** For a streaming request the proxy sets `stream_options.include_usage`, passes
  the stream through line by line, unchanged and unbuffered, and takes `usage` from the final
  chunk; a JSON response is read whole. If the client hangs up mid-stream (an agent aborted at
  its turn or time limit), the proxy keeps reading to the end, since the provider bills the
  whole call and only the end says how much. After the agent stops, the runner waits for calls
  still in flight before reading the totals.

Each call appends one record to the item's `usage.jsonl`:

| Field | Meaning |
| --- | --- |
| `seq`, `ts`, `role` | Call order, time, and `executor` or `advisor` |
| `model` | Model id as sent |
| `prompt_tokens`, `completion_tokens` | From the provider's `usage` (streaming: `stream_options.include_usage` is forced on); the Responses API's `input_tokens`/`output_tokens` are read too |
| `cached_tokens`, `reasoning_tokens` | `prompt_tokens_details.cached_tokens` (or Moonshot's top-level `cached_tokens`) and `completion_tokens_details.reasoning_tokens`; 0 when absent |
| `latency_ms`, `status` | Wall time and HTTP status |

- **Only providers that report usage.** Counts come from the provider, never from estimates,
  so every endpoint must return `usage`, including in streams. Before a batch starts, the
  runner sends one tiny streaming request ("Reply with the single word OK.", no
  `stream_options`, so the proxy's forcing is checked too) through the proxy to each distinct
  endpoint the batch uses (the executors and, for advisor arms, `advisor`), and refuses to run
  if a response has no `usage`; those calls go to `runs/<experiment>/preflight/`.
  `bench run --no-preflight` skips it, for the mock server and tests only (the mock server
  also answers the preflight without using up a recording). If a successful (2xx) call during
  a run still arrives without usage, the item fails (it is not recorded as done; the infra
  retry applies), so no estimated number enters the results. Error responses without usage
  (a 503, say) are not billed calls and are not recorded.
- **Token budget.** `limits.max_tokens` (optional) caps prompt plus completion tokens over all
  roles of an item. Once the item's total reaches it, the proxy answers further calls with
  HTTP 403 (`type: token_limit`; clients retry 429 and 5xx, and a retry cannot help), the
  agent stops on the error, and the runner records the exit reason `token_limit`. The call
  that crosses the budget completes and counts. An unset `max_tokens` is left out of the
  config hash, so existing hashes did not change.
- Each attempt starts a fresh `usage.jsonl`; the ledger keeps the last attempt, so tokens spent
  by an attempt that failed on infrastructure are not counted.
- `usage.jsonl` is the fourth contract (`usage.schema.json`, `UsageRecord`). The runner fills
  `AgentResult.usage` (`RoleUsage` per role that made calls; empty when not metered) from it.
- **Cost.** The experiment's `prices` (`<models key>: {input_per_mtok, output_per_mtok,
  cached_input_per_mtok?}`, USD per million tokens, not in the config hash) price each role
  by the model it used. Cached prompt tokens use the cached price when one is set; reasoning
  tokens are part of the completion tokens and use the output price. `cost_usd` is null when a
  model that made calls has no price.
- **Where the totals go.** Ledger columns `executor_prompt_tokens`,
  `executor_completion_tokens`, `advisor_prompt_tokens`, `advisor_completion_tokens`,
  `model_calls`, `cost_usd` (null when not metered; older ledgers gain them on open). MLflow:
  the same as item metrics, and per arm `tokens_sum`/`tokens_median`,
  `advisor_tokens_sum`/`advisor_tokens_median`, `cost_usd_sum`/`cost_usd_median`, and
  `cost_usd_per_resolved`. The item trace's agent span carries the proxy's counts
  (`proxy.<role>.*`) next to the totals of the agent's own model spans (`agent.tokens.*`, as
  pi reports them), so the two can be compared. `bench report` shows tokens per item (all
  roles and advisor), total cost, and cost per resolved task per arm; the arm's cost is shown
  only when every done item has one.
- The advisor plugin's `advisor_request`/`advisor_response` events stay the record of what was
  sent (exposure); the proxy is the record of what was used (cost). Both count the same advisor
  calls, which gives a cross-check.

**Event types.** Every event also has `schema_version`, `seq` (monotonic per run from 0),
`ts` (Unix seconds), and `type`.

| Event | Key fields |
| --- | --- |
| `policy_rendered` | once per advisor run, first: prompt hash, the executor guidance as appended to the system prompt, the consult tool description (null when not registered) |
| `consult_requested` | the executor's question, turn |
| `trigger_fired` | intervention, reason, turn |
| `brief_built` | level, tokens, identifiers redacted, role-map size, role map (placeholder to identifier, for the placeholders in this brief) |
| `advisor_request` | request id, input tokens (an estimate: 4 characters a token), brief text, prompt hash |
| `advisor_response` | request id, output tokens, cached tokens, prompt and reasoning tokens (the provider's counts; null when not reported), finish reason (`stop`, `length`, ...; null when not reported), latency (ms), the advice text exactly as received |
| `advisor_error` | request id, message, HTTP status (null when no response came back), latency (ms) |
| `advice_applied` | request id, turn it was injected at, the exact text the executor was given |
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
- **Task selection** (optional): `split: dev|test` runs only that split of the manifest, and
  `task_ids: [...]` only those tasks (after `split`). Selection is not in the config hash; it
  picks which items run, not how they behave. Prompt work uses `split: dev`.
- **Endpoints and secrets**: `bench` loads `NAME=value` lines from the repository's `.env`
  (gitignored; `--env-file` or `BENCH_ENV_FILE` to change; exported variables win), and
  `.env.example` lists the names. A model's `api_key_env` and `header_env` (header name to
  variable name) name secrets, never hold them; `headers` holds non-secret header values.
  `${VAR}` also works in mapping keys, so header names can stay out of committed YAML too.
  Only the metering proxy on the host uses the secrets; agent containers never get them.
- **Limits**: `wall_minutes` and `max_turns`, and optionally `max_tokens`, the per-item token
  budget enforced by the metering proxy (see [Token metering](#contracts-and-interfaces)).
- **Prices** (optional): `prices: {<models key>: {input_per_mtok, output_per_mtok,
  cached_input_per_mtok}}` in USD per million tokens (the cached price is optional), for
  `cost_usd`. Keys must be in `models`. Not part of the config hash: cost is derived from the
  metered tokens, so a price can be corrected after a run.
- **Arms** name an `executor` model, which must be a key in `models`. Advisor arms consult
  the model under the key `advisor`. Arm names must be unique.
- **Prompt sets** (optional): `prompts:` names sets of the five prompt slots, as a directory
  or as another set with some slots replaced; `default` is `prompts/default/`. See
  [Research design](#research-design-help-policies).
- **Sweeps**: a list in `advisor.level`, `advisor.max_consults`, or `advisor.prompts`, or a
  list of lists in `advisor.interventions`, expands into one arm per combination, named by
  suffix: arm `A2` with `level: [L1, L2]` becomes `A2-L1` and `A2-L2`;
  `interventions: [[consult], [consult, stuck]]` gives `A2-consult` and `A2-consult+stuck`.
  A flat `interventions` list is one value, not a sweep.
- **Execution** (optional): `parallel` (work items at once, default 1), `cpus` and
  `memory_gb` (caps per container, default 4 and 8), `retries` (extra attempts after an
  infrastructure error, default 1), `grade_minutes` (default 30). Not part of the config hash.
  `bench run --parallel N` overrides `parallel`.
- **Config hash**: 16 hex chars of SHA-256 over the arm (minus its name), the agent entry,
  the limits, the executor and advisor model settings (minus `base_url`, `headers`, and
  `header_env`), and for an advisor arm the prompt set's text hash in place of its name.
  Renaming an arm or a prompt set, or moving a server or a prompt file, keeps results;
  changing behaviour invalidates them. Unset optional limits (`max_tokens`) are left out, so
  adding an optional limit keeps existing hashes; prices are not hashed.

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
| `bench tasks import [--dataset mini\|full] [--instance ID]` | Import Multi-SWE-bench C++ instances as candidates (dataset pinned to a revision) |
| `bench tasks build NAME [--only ID] [--parallel N] [--jobs N]` | Build arm64 task images; resumes |
| `bench tasks validate NAME [--runs 2] [--parallel N]` | Gold-patch validation; resumes |
| `bench tasks freeze NAME --version V --out F [--smoke 3]` | Write the frozen manifest (with split and dropped instances) and a smoke subset |
| `bench run EXP.yaml [--parallel N] [--arm A] [--task ID] [--mlflow URI] [--final] [--proxy-host H] [--dry-run]` | Batch run; resumes where it stopped. Always tracked in MLflow (`--mlflow`, `MLFLOW_TRACKING_URI`, default `http://127.0.0.1:5050`); refuses to start if the server is down. `--no-mlflow` is for harness tests and CI only. Tasks in the `test` split need `--final`. Model calls go through the metering proxy (binds `--proxy-host`, default 127.0.0.1) after a usage preflight; `--no-preflight` is for the mock server and tests only |
| `bench run EXP.yaml --arm A2 --task ID --debug` | One task with live logs; keeps the container afterwards |
| `bench shell ITEM` | Open a shell in a kept container |
| `bench replay ITEM` | Step through a stored run's events: turns, triggers, briefs, advice |
| `bench score EXP.yaml` | Re-run scorers over stored runs |
| `bench report EXP.yaml [--csv F] [--pairs-csv F] [--baseline A0] [--ceiling A4]` | Variants tried and final batches; per-arm table from the ledger (stale config hashes ignored); paired comparisons with the baseline; Pareto front against advisor cost; per-item and per-pair CSV. Plots to come |
| `bench schemas [--check]` | Regenerate (or verify) the contract JSON Schemas |
| `bench mock-server --recordings F [--upstream URL]` | Serve recorded completions; record from a real endpoint |

## Task pipeline

The pipeline turns benchmark instances into a frozen, arm64-validated task set; only tasks
that pass validation twice are kept. Work files (candidates, build and validation records,
logs) go in `runs/tasks/<name>/`; dataset downloads and git mirrors in `.cache/`. Practical
notes, measurements, and problems found: [task-pipeline.md](task-pipeline.md).

1. **Import** (`bench tasks import`). Multi-SWE-bench `mini` first (50 C++ instances: nlohmann/json
   21, fmt 17, simdjson 8, Catch2 4); `full` is registered too and adds cpp-httplib, which has
   no recipe yet. Each dataset is pinned to a Hugging Face commit (and checksum for `mini`).
   The problem statement is the resolved issues' titles and bodies; the pull request's own
   text describes the fix and is left out. Upstream's fixed tests (fail, skip, or absent
   before; pass after) and pass-to-pass tests are kept for validation.
2. **Build** (`bench tasks build`). One recipe per repository in `tasks/repos/<org>__<repo>/`:
   a Dockerfile and `recipe.yaml` (git URL, log parser, GCC version by PR number, following
   the upstream harness; its `gcc:latest` is pinned to 14). Every image uses a pinned CMake
   (3.31, arm64 or x86-64 tarball, checksum-verified) instead of upstream's x86-64-only
   CMake 3.14 download. The base commit's files come from a local mirror into a fresh
   one-commit repository in `/testbed`, so the fix is not reachable in history. Submodules
   are checked out at their recorded commits (recursively) as nested one-commit
   repositories, since some build scripts look for their `.git`. The project
   is configured and built in `/build` at image build time, so grading rebuilds incrementally.
3. **Validate** (`bench tasks validate`). Fresh containers run the tests with the test patch
   alone ("before") and with the gold patch ("after"), twice. The eval command
   (`/opt/lso/run-tests`) deletes the previous build's linked outputs (keeping object files,
   so the build stays incremental; otherwise a target that no longer compiles would leave its
   old binary to be tested), reconfigures, builds with `-k` so one broken test target does
   not hide the rest, and runs ctest. Test lists are re-derived from these runs, over upstream's
   tests: fail-to-pass = passes after, not before; pass-to-pass = passes both. A candidate is
   dropped if any upstream fixed test fails with the gold patch, any test differs between
   runs (flaky), no test goes from failing to passing, a patch does not apply, or a run times
   out. Upstream pass-to-pass tests that fail here with the gold patch are left out and
   recorded. (Upstream's lists come from x86-64 runs where one compile error fails the whole
   suite, so many of its "fixed" tests are really pass-to-pass.)
4. **Freeze** (`bench tasks freeze`). Candidates over 20 min to build or 10 min to test are
   dropped. The manifest lists every task with image ID, test lists, base commit and its
   date, and split; and every dropped candidate with the reason. The split is a fixed-seed
   shuffle within each repository (default half to `test`; a repository with one task goes
   to `dev`). A smoke subset (the three quickest `dev` tasks from different repositories) is
   written next to it.
5. **Agent layer.** Not a per-task image: the agent's bundle (Node, pi, and the advisor plugin)
   is mounted read-only from a Docker volume at run time (see the pi adapter).

Task images are local to the machine that built them: the manifest pins each by image ID, and
the runner refuses a missing ID rather than using a rebuilt image, since a rebuild needs
validating again.

**Internal task format** (`tasks.Task`): `id`, `image` and `image_id`, `workdir` (default
`/testbed`), `problem_statement`, `test_patch` (applied only at grading), `gold_patch`,
`eval_command` (run in `workdir` at grading), optional `tests` (parser name, `fail_to_pass`,
`pass_to_pass`), and provenance (`repo`, `base_commit`, `base_date`, `split`). With `tests`,
a task is resolved when every listed test passes in the parsed output, whatever the exit
code; without it (toy tasks), when `eval_command` exits 0. A manifest is `version`, `source`,
`tasks` (unique ids), and `dropped`.

The manifest is the only input experiments see. A new manifest version is a new task set,
and results from different versions are never mixed.

## Storage, resume, and reproducibility

A small SQLite ledger tracks what has run; MLflow stores what happened.

- **Ledger.** One row per work item, keyed by experiment, arm, task, seed, and config hash.
  It stores status, attempts, and the MLflow run and trace IDs, and the metered tokens per
  role, model calls, and cost (nullable columns, added in place to older ledgers). Changing an
  arm's config changes its hash, so stale results are never reused. A second table,
  `sessions`, has one row per `bench run` batch: start time, split, number of test tasks, and
  `--final`.
- **Item directory.** `runs/<experiment>/<arm>/<task>/seed-<n>/` holds `advisor.json`,
  `result.json`, `patch.diff`, `events.jsonl`, `grade.log`, and for metered agents
  `usage.jsonl`, whether or not MLflow is on. The ledger is `runs/<experiment>/ledger.sqlite`;
  preflight calls are recorded in `runs/<experiment>/preflight/`.
- **MLflow is mandatory.** Every experiment run is tracked; `bench run` checks the server's
  `/health` first and will not start without it. MLflow is a core dependency.
- **MLflow layout.** One MLflow experiment per experiment file; one parent run per arm
  (tagged with the config hash and reused on resume) holding the pinned inputs as params, the
  harness's git commit and whether the checkout was dirty as tags, and summary metrics updated
  after every batch (`resolve_rate` with its Wilson interval, items done and failed, median
  turns and duration, `exit_<reason>` counts, token and cost sums and medians, cost per
  resolved task); one child run per task and seed with `resolved`, `turns`, `duration_s`, the
  metered token counts, `model_calls`, and `cost_usd`, and the item directory as artifacts.
- **Traces.** Each item's run has one trace: the item, the agent (with the diff and exit
  reason), and grading (test outcome counts). Adapters add the agent's spans from their own
  logs after the run (`AgentAdapter.spans`): for pi, a span per turn with its model calls
  (messages in and out, tool calls, token counts as the model server reported them) and tool
  calls (arguments, result, error). The agent span also has the metering proxy's counts
  beside the sum of the agent's own, for comparison. pi's JSON events have no times for tool calls, so the
  harness extension `agents/pi/timeline.ts` records turn, model, and tool start and end times.
  Traces are built after the fact, so every agent gets them, with or without the plugin.
  Advisor arms add a span per consult from `events.jsonl` (`tracing.advisor_spans`): `advisor:
  <trigger>` (inputs: reason and turn; output: the injected text; attributes: level, brief
  tokens, identifiers redacted, request id, prompt hash, applied turn) with a `brief` span (the
  exact text sent) and an `advisor` model span (system and user messages, the advice, tokens,
  latency, or the error), plus a span for a consult refused for budget. A consult is placed
  under the deepest span open when it started: the `consult` tool span for the executor's,
  between turns for a turn-end trigger. Long values are clipped to 20k
  characters, keeping both ends; the artifacts keep everything.
- **Pinned inputs.** Each run logs the manifest version, image digests, local model ID and
  MLX quantization, advisor model ID and reasoning effort, plugin and harness versions, and the seed.
- **Known non-determinism.** MLX sampling is not bit-exact across runs, and the advisor API
  can change behind the same model ID. Seeds reduce variance but do not remove it; the
  analysis relies on multiple seeds.
- **Secrets and network.** API keys live only in environment variables on the host, where
  the metering proxy adds them to forwarded calls; agent containers never see them. The proxy
  binds 127.0.0.1 (Docker Desktop reaches it through `host.docker.internal`). The MLX server
  binds to the host interface the VM can reach, not to the wider network.

## Repository layout, packaging, and testing

```
llm_second_opinion/
  harness/          Python package (pip-installable), CLI `bench`
  plugin/
    advisor-core/   TypeScript, no pi imports
    pi-binding/     pi extension package
  schemas/          JSON Schemas: run config, events, results (generated)
  tasks/manifests/  frozen task sets
  tasks/repos/      per-repository image recipes (Dockerfile, recipe.yaml) and shared scripts
  experiments/      example YAML files
  docs/             this spec and the roadmap
  scripts/          dev helpers (local MLflow server)
```

- **Mock model server.** An OpenAI-compatible `/v1/chat/completions` (streaming and not)
  that replays recorded completions from JSONL in file order, single-threaded. With
  `--upstream`, requests past the end of the file are proxied to a real endpoint and
  appended to it (API key from `UPSTREAM_API_KEY`). Lets contributors and CI run end-to-end tests without a GPU or API keys.
  A recording without `usage` is answered with a made-up one (about four characters per
  token, marked `"mock_estimate": true`), in the JSON body or the final stream chunk, so the
  metering proxy accepts it; a usage preflight request (header `X-LSO-Preflight`) gets a
  canned reply without using up a recording. `test_pi_docker.py` runs pi against it through
  the proxy, both bound to 127.0.0.1.
- **Toy task set.** `tasks/toy/` (image) and `tasks/manifests/toy-v1.yaml`: two one-line
  shell bugs with test and gold patches. `experiments/toy.yaml` runs them with the `gold`
  agent, exercising containers, grading, the ledger, and the report with no model.
  Docker-backed tests (`test_docker.py`) build the image and skip when no daemon is running.
- **Smoke task set.** Three quick validated C++ tasks (`<manifest>-smoke.yaml`, written by
  `bench tasks freeze`) for end-to-end runs with the mock server, covering the full lifecycle
  including grading. Their images take minutes to build, so CI keeps using the toy set.
- **Tests.** Unit tests on both sides, plus contract tests that check the plugin's events
  against the schemas. Plugin tests are type-checked (`tsconfig.test.json`) before vitest runs.
  `test_pi_docker.py` runs pi with the plugin on `toy-add` against one mock server for both
  roles, through the metering proxy (`fixtures/pi-toy-add-consult.jsonl`: the executor calls
  `consult`, the advisor answers, the executor fixes the bug), and checks the events, the
  redaction at L1, the advice reaching pi, metering per role, and the trace.
- **Generated types.** `plugin/scripts/gen-types.mjs` merges `schemas/*.schema.json` into
  `advisor-core/src/contracts.ts` (committed); `pnpm check:types` fails on drift.
- **Local MLflow.** `scripts/mlflow-server.sh` runs a tracking server on 127.0.0.1:5050 with
  SQLite metadata and artifacts under `.mlflow/` (gitignored).

## Open questions


- [ ] Container runtime: Colima is the default because it exposes the Docker API the Python
      SDK expects. Revisit Apple's `container` tool if Docker API support is not needed.
- [x] The executor is served from the user's own second machine (an OpenAI-compatible endpoint
      in `.env`), not by MLX on the Mac. It is trusted, local infrastructure: its traffic is
      not exposure, and only what reaches the cloud advisor is measured. (Resolved 2026-10-02.)
- [x] Where the brief builder's role map lives across a session, so advice maps back
      correctly after context compaction. In the plugin's memory for the whole pi process;
      advice is mapped back before injection, so compaction never sees placeholders. (A pi
      session resume would start a new map; runs do not resume.)
- [ ] Redaction is heuristic: a project name that reads as an ordinary word (`parse`, `value`)
      stays in prose at L0–L2, and the same file can get two placeholders (`a.hpp` and
      `src/a.hpp`). The leakage scorer should measure what slips through.
- [ ] Whether the re-identification attacker runs at scoring time only, or also as a live
      check that blocks a brief before sending.
- [ ] CLI name: keep `bench`, or rename (e.g. `lso`)?
- [ ] Safe `parallel` for the executor endpoint: measure throughput at 1, 2, 4 concurrent sessions.
- [ ] Search budget in rollouts, from the pilot's run time and variance.
- [ ] Does the proposer get A4's successful trajectories on the same task, or only the
      candidate's own runs? Showing A4 helps reflection but moves the search toward
      imitating the advisor.
- [ ] `dev`/`test` ratio, given how many tasks survive arm64 validation (power analysis once the
      pilot gives a variance estimate).

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
| 2026-09-27 | The experiments optimise the help policy: prompt sets (five slots, hashed by text) and approach (initiative, content, budget), both in config. Tune on a `dev` split, report on held-out `test`; arms compared in pairs. |
| 2026-09-27 | Automatic prompt search (GEPA-style reflective evolution, `bench search`) on `dev` (`train`/`val`), with hand-written sets as starting points. One fixed model pair (Qwen3.8 executor, Kimi K3 advisor); prompts tuned to it. |
| 2026-09-27 | Multi-SWE-bench `mini` (C++) first, `full` registered for later; datasets pinned to a Hugging Face commit. Task creation date recorded as the base commit's date. |
| 2026-09-27 | Our own per-repository Dockerfiles and recipes (not upstream's generated ones); pinned CMake 3.31 in every image; upstream `gcc:latest` pinned to 14. Images are a fresh one-commit repo with a prebuilt `/build`. |
| 2026-09-27 | Grading by per-test lists (`Task.tests`, ctest parser) instead of an exit code; fail-to-pass and pass-to-pass re-derived from arm64 before/after runs over upstream's tests. |
| 2026-09-27 | Manifests pin task images by local image ID; freeze caps: build 20 min, tests 10 min; split `dev`/`test` half and half within each repository (seed 0). |
| 2026-09-29 | pi adapter: CLI JSON mode, not the SDK. pi has no turn-limit flag, so a harness extension enforces it; exit reason from the timeout, that extension, pi's exit code, and the final message. |
| 2026-09-29 | Agent bundle mounted read-only from a Docker volume instead of a derived image per task (~560 MB each, not shared between task images). Toy image moved to Debian for glibc. |
| 2026-09-29 | `agents:` entries implemented; the entry's contents (not its name) are in the config hash, so existing hashes changed. Builds use `--provenance=false`, so unchanged rebuilds keep their image IDs. |
| 2026-09-29 | MLflow is mandatory for every experiment run (default local server, health check before starting; `--no-mlflow` only for tests and CI) and a core dependency. |
| 2026-09-29 | Item traces are built by the harness from the agent's logs after each run (pi: JSON events plus a timeline extension), not only by the plugin's OTel exporter; arm runs get summary metrics and the git commit. |
| 2026-10-02 | Executor (Qwen3.8 27B) served from an OpenAI-compatible endpoint configured in a gitignored `.env`, which `bench` loads; endpoints gain `headers` and `header_env` (secret headers by variable name), excluded from the config hash with `base_url`; `${VAR}` interpolates mapping keys. pi options gain `thinking_level_map` and `compat`. |
| 2026-10-02 | Experiments select tasks with `split` and `task_ids`, outside the config hash. `experiments/baselines.yaml`: A0 and A4 on `dev`, 3 seeds. |
| 2026-10-02 | Pilot contracts fixed before the build splits up: prompt-set name in `advisor.prompts`, resolved `RunConfig.prompts`; budget knobs `max_answer_tokens`, `cooldown_turns`, `periodic_every`; interventions `on_test_failure`, `periodic`; events `consult_requested`, `advisor_error`, `advisor_request.prompt_hash`; `usage.schema.json`; `AgentResult.usage`; exit reason `token_limit`. `schema_version` stays "1": nothing with advisor data has been recorded yet. |
| 2026-10-02 | Task set `mswe-mini-cpp-v2` replaces v1: two deleted images rebuilt with new IDs and validated again; otherwise identical. Experiments use v2; v1 stays as frozen. |
| 2026-10-02 | Prompt sets built: `prompts:` maps names to a directory (all five `<slot>.md`) or `{base, <slot>: file}` (chains allowed, cycles rejected); implicit `default` is `prompts/default/`. Placeholder lists fixed per slot (see Research design) and checked at load; `brief` adds `{{hypothesis}}` and `{{trigger}}`, `advice_injection` adds `{{consults_left}}`, and `{{advice}}` is required. Slot texts are normalised (LF, trailing whitespace trimmed) before hashing. The config hash uses the set's text hash in place of its name; arms without an advisor keep their hashes. The consult tool is `consult(question, tried, hypothesis)`. |
| 2026-10-02 | `advisor.prompts` sweeps (a list) and `advisor.interventions` sweeps (a list of lists); the interventions suffix is the names joined by `+`, `none` when empty. |
| 2026-10-02 | Honesty guard: `bench run` refuses tasks in the `test` split without `--final`; batches are recorded in a ledger `sessions` table, and final ones are tagged `lso.final` in MLflow. `bench report` prints the number of variants tried (distinct config hashes in the ledger). |
| 2026-10-02 | Paired comparisons in `bench report`: each arm vs `A0` on shared (task, seed) items, paired bootstrap over tasks (10,000 resamples, seed 0), McNemar exact, relative lift, share of the A0–A4 gap closed with a bootstrap CI, and a Pareto front against `cost_usd` or advisor tokens when the ledger has them. Standard library only (numpy is installed only as an MLflow dependency). |
| 2026-10-02 | Metering proxy built (`metering.py`): its own threaded server rather than the mock server's upstream mode (which buffers and drops streaming); random per-attempt item ids; the proxy adds API keys and secret headers on the host, so agent containers get no secrets and `advisor.json`/`models.json` hold only proxy URLs. Binds 127.0.0.1, which Docker Desktop containers reach via `host.docker.internal` (`--proxy-host` for other VMs). Adapters declare `uses_models`; `gold` is not metered. |
| 2026-10-02 | Usage rules: a 2xx call without usage fails the item (error replies without usage are not recorded); a stream the client abandons is read to the end for its usage; each attempt starts a fresh `usage.jsonl`. Usage preflight through the proxy before a batch, `--no-preflight` for the mock server and tests only; the mock server answers the preflight without using a recording and makes up marked usage for recordings without it. |
| 2026-10-02 | Token budget `limits.max_tokens` (prompt + completion over all roles): once reached the proxy answers 403 `token_limit` and the runner records exit reason `token_limit`. Unset optional limits are left out of the config hash, so existing hashes are unchanged. |
| 2026-10-02 | Cost: `prices` per models key (input, output, optional cached input, USD per million tokens), outside the config hash; reasoning tokens priced as output; `cost_usd` null if a model that made calls has no price. Ledger columns `executor_prompt_tokens`, `executor_completion_tokens`, `advisor_prompt_tokens`, `advisor_completion_tokens`, `model_calls`, `cost_usd` (added to old ledgers in place); MLflow item metrics, arm sums and medians, and proxy vs agent counts on the agent span; `bench report` shows tokens and cost per arm and per resolved task. |
| 2026-10-02 | Advisor plugin built: advisor-core (config check, `{{name}}` rendering with each slot's placeholder list, trigger engine, redaction with a per-run role map, L0–L3 brief builder, OpenAI-compatible client, session) and pi-binding (pi extension). Levels, trigger rules, cooldown and budget semantics as in [The advisor plugin](#the-advisor-plugin); `{{code}}` is empty at L0 and L1. `max_consults` counts attempts, including failed ones. |
| 2026-10-02 | Advice injection in pi: consult tool → tool result; `plan` → `before_agent_start` custom message (before the first model call, from the issue alone, not a review of an executor plan); other harness triggers → a `turn_end` custom message entry with `continue: true`. `executor_guidance` via `before_agent_start`'s `appendSystemPrompt` (no CLI flag). An advisor arm's `advisor.json` must carry its prompt set; the plugin has no built-in prompts. |
| 2026-10-02 | The plugin is bundled with esbuild from its TypeScript sources into one extension file in a first stage of the pi bundle image (named build contexts), not built on the host. The plugin keeps its own `advice.jsonl` (system and user messages, advice received and injected) next to the contract events, since `advisor_response` carries no text. Advisor spans in item traces come from `events.jsonl` and `advice.jsonl`. |
| 2026-10-02 | `advisor_response.advice_text` (as received, placeholders intact) and `advice_applied.injected_text` (what the executor saw) join the event contract, so advice uptake and prompt search read `events.jsonl` alone; the plugin's `advice.jsonl` stays as a debugging record. |
| 2026-10-02 | "Local" means hardware the experimenter controls, not only the Mac running the harness. The executor runs on the user's second machine and is trusted: exposure counts only what reaches the cloud advisor (`advisor_request`), and the executor's traffic is metered for cost and time only. |
| 2026-10-03 | Plugin on the data-retention contracts. `policy_rendered` is emitted by the session once, before anything else (the binding calls it at `before_agent_start`; every session entry point also calls it, so it comes first in any binding). `brief_built.role_map` holds only the placeholders that appear in that brief; `advice.jsonl` gets the whole map as of each consult. `advisor_request.input_tokens` stays the estimate; the provider's `prompt_tokens` and `reasoning_tokens` go on `advisor_response`. |
| 2026-10-03 | Advisor calls send `X-LSO-Request-Id` and, when the endpoint sets them, `temperature`, `top_p` and `seed` (from `sampling_seed`); the request body is otherwise unchanged. `advisor_error.status` is null for a network error, timeout or abort, and the HTTP status otherwise, including a 2xx with an unreadable body. |
| 2026-10-03 | `advisor_response.finish_reason` (the provider's `choices[0].finish_reason`) joins the event contract: a real Kimi K3 run with `max_answer_tokens: 400` ended mid-sentence at exactly 400 completion tokens, 43 of them reasoning, with nothing in the events to show it. Advice cut at the limit is still injected, with a visible truncation marker; the default `advisor_system` now states the limit and that reasoning counts toward it. |
| 2026-10-03 | Template rendering drops empty sections (a heading line plus a placeholder that renders empty) in every slot, so briefs carry no empty headings such as "What they have tried:" on a turn-0 `plan`. |
| 2026-10-03 | Size limits are prompt targets, not truncation: the consult tool and `advisor_system` state the targets (policy text, tunable); hard ceilings (`max_brief_tokens` ~3000, answer ~4000) are safety nets, logged when hit. Executor-written brief fields get a per-field target and ceiling. |
| 2026-10-03 | Anti-delegation rules on by default, strictness an arm setting: earned consults (own actions + cooldown, `consult_refused`), required `tried`/`hypothesis`, `max_advice_code_lines`; dependence scorers (advice-copy share, own work before first consult, consult rate, brief synthesis share) guard the optimizer. |
