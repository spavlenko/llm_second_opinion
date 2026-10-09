# Results (interim, 2026-10-09)

Interim results on the `dev` split only; nothing here has touched `test`. Setup and rules are
in [spec.md](spec.md); the run-by-run findings are in [lab-notes.md](lab-notes.md).

## Question

Code that may not leave the machine is fixed by a local model. Can a cloud model's second
opinion, shown only what the local agent chooses to send, raise the local resolve rate
enough to matter?

- **Executor:** Qwen3.8 27B, local, in pi, in a Docker container per task.
- **Advisor:** Kimi K3 (Kimi Code plan), consulted through the pi extension; every brief is
  recorded.
- **Tasks:** 25 C++ Multi-SWE-bench `dev` tasks (fmt, nlohmann/json, Catch2) chosen for
  headroom: the local model alone is flaky on them, and Kimi as the agent mostly solves them.

## Arms and the gate

| Arm | What runs | Resolved |
| --- | --- | --- |
| A0 | Qwen alone, one run (9 seeds per task) | 59 / 225 (26%) |
| `L-best3` | three A0 runs, a local picker (build + existing tests) chooses one patch | 25 / 74 groups |
| A4 (ceiling) | Kimi as the agent | 92.8% (68.7 of 74) |

The gate (fixed before the runs): a help policy must close at least 25% of the gap between
`L-best3` and A4, gap closed = (H − `L-best3`) / (A4 − `L-best3`). `L-best3` is the bar
because three local runs cost no cloud tokens and expose no code.

## What we tried

| Policy | Kimi sees | Resolved | Gap closed |
| --- | --- | --- | --- |
| `review`: Kimi ranks the 3 local patches | issue + candidate diffs + local checks | 28 / 74 | 7% |
| Perfect picker (oracle, upper bound for any `review`) | — | 38 / 74 | 30% |
| `H-phase`: one run, up to 3 hint consults (plan, orient, stuck) | issue + recent command names + agent notes | 25 / 75 | 0% (90% CI −26% .. 21%) |
| `H-evidence`: same, richer brief | + command outputs, executor reasoning, edits | 19 / 60 | −12% (90% CI −46% .. 11%) |

- **Picking is capped low.** Even a perfect pick among three local patches closes only 30%.
  The losing patches mostly fail to build against the hidden tests, which call an API the fix
  introduces; a diff review cannot see that.
- **Hints help a single run, not the bar.** `H-phase` is +5.3 over one A0 run (19.7 expected),
  11% of the A0–A4 gap, but no better than three free local runs plus a picker. Gate: no-go.
- **Kimi was guessing.** The `H-phase` briefs never carried a command's output, the
  executor's reasoning (pi's thinking parts were dropped) or its edits; median ~1k characters
  beyond the issue. On `json-3601` Kimi told the agent to "check the output of your git log".
- **With the evidence it diagnoses.** The evidence brief (5–17k characters) gave Kimi what the
  agent saw. On `fmt-2158` it caught a signed `difference_type` defect (2 of 3 resolved); on
  `json-3601` it still steered toward editing the examples until the advisor prompt said to
  restore the behaviour the issue shows, after which all three seeds went for the library
  fix (the upstream one).
- **But better diagnoses did not resolve more.** `H-evidence` resolves 19 of 60 paired groups
  (25 tasks), against `L-best3`'s 23, `H-phase`'s 20 and 16.4 expected for one A0 run: −12%
  of the gap (90% CI −46% .. 11%), 7% of the A0–A4 gap. The first 18 runs (8 resolved) were
  noise. 15 runs are missing (Kimi's plan quota ran out twice), but even if all of them
  resolved the arm could not reach the 25% bar (36 of 74). Gate: no-go again.

## Optimization loop (2026-10-09)

After the second no-go we stopped tuning the brief and read the runs: what did Kimi say, and
what did Qwen do with it? The loop tunes on 5 `dev` tasks (fmt-3248, json-2019, json-2225,
json-3564, simdjson-644), 2 seeds per arm; the other 20 `dev` tasks are held out.

**What we had.** On these 5 tasks: A0 8 / 45 (18%), `H-phase` 7 / 15, `H-evidence` 5 / 11.
The advice was often right, but nothing measured whether Qwen used it.

| Iteration | What changed | Resolved | Why |
| --- | --- | --- | --- |
| 1 `H-case` | Case protocol: Qwen investigates, files a report before its first edit (`report_gate`); Kimi answers with ranked causes, an experiment for each, acceptance criteria | 4 / 10 | Qwen reported once and never came back; failures happened after the report |
| 2 `H-case-close` | Closing report before stopping (`closing_report`) | fmt 0 / 2 | Kimi said 3 times to drop a special case for a visible test that encodes the bug; Qwen refused: "do not change existing tests" |
| 3 `H-case-tests` | Guidance: a test that encodes the bug stays failing and is named | fmt 2 / 2 | Advice does not override a rule in the task prompt; the guidance must say it |
| 4 `H-case-uptake` | Experiment before editing (`experiment_report`), read-back, Kimi's acceptance check, "recall is not evidence" | 4 / 10 | Uptake fixed (right and followed 8 / 16 → 18 / 23); the losses moved to untested contexts and approved wrong fixes |
| 5 `H-case-users` | Acceptance check from user code over every variant | 0 / 4 | Kimi could not guess what the hidden tests use (json-3564: an old `erase` bug reached by the suite over `ordered_json`) |
| 6 `H-case-back` | Stricter closing review (name the strongest reason the maintainers would reject the change, then check it); a come-back prompt after 25 quiet turns | **8 / 10** | The review caught a false "all green" claim and endorsed only checked fixes |

- **Measure uptake, not only advice.** `bench uptake` (an offline judge that sees the upstream
  fix) grades each consult: was the advice right, and did the final patch follow it? Right
  advice was followed half the time before iteration 4 and four times in five after.
- **Gates work, hints do not.** Qwen obeyed every gate that refused a tool call
  (`report_gate`, `experiment_report`), and declined every optional hint. The come-back
  prompt fired 3 times; Qwen never consulted after it, once writing "I'm stuck on a
  sub-problem … I'm not going to consult … let me debug first". A small model can name being
  stuck and still not act on it.
- **The activity-based `stuck` trigger misses side quests.** A new error counts as progress,
  so 23 scratch programs in `/tmp`, each with a different preprocessor error, looked like
  progress. Next: detect stuck from the task (tests and repro unchanged, work only outside the
  repository, repeated tool failures, stalls, a turn-50 checkpoint), tune it offline on the
  logged runs, and make the report a gate rather than a hint.
- **Caveats.** 8 / 10 is on the tuning tasks, 2 seeds each; one of the 8 (json-3564 s0) came
  from Qwen recalling the upstream code, not from advice. It counts only once held-out tasks
  confirm it. Kimi cost: ~9k tokens in and 3.5k out per run (3 consults).

### Where Qwen's runs fail (offline, 2026-10-09)

Over the logged Qwen dev runs, before spending Kimi on stuck handling:

- **Qwen stops; it does not get lost.** 91% of failed runs end with Qwen declaring the task
  finished; 8% hit the turn limit. 70% of finished runs saw the visible tests pass after their
  last edit and still resolve only 36%: the miss is what the hidden tests check (a user
  namespace, a sibling type), not skipped verification.
- **Exploring is not being stuck.** LivePlan-style phase stagnation does not separate runs;
  long scratch work goes with success (71% vs 27% at turn 20). A burst of failed calls does
  (17% vs 31%): `stuck_report` gates on it.
- **Two published fixes do not transfer.** Rewriting failed calls (Fail-Fast): Qwen 27B
  repeats one verbatim 0.4% of the time (54% for the paper's small models). Restarting on the
  burst within the same turn budget: −1.0 pt by counterfactual; runs still going at turn 60
  resolve 30%, more than a fresh run.

So the lever is the stop: what the closing review and the acceptance check make Qwen test
before it says "done".

## Cost and exposure

| Run | Consults | Kimi tokens in / out |
| --- | --- | --- |
| `review` on gate-a0 | 70 | 471k / 156k |
| gate-phase | 195 | 172k / 126k |
| gate-evidence | 154 | 348k / 173k (~$3.4 at API prices) |

All three are at L3 (unredacted): the gate measures whether help works at all before paying
for redaction. Every brief is stored with its run.

## Lessons for the harness

- **Measure the advisor's input first.** Two arms ran before anyone read what Kimi was shown.
- **Variance dominates single seeds.** A0 per task swings between 0 and 1 across seeds;
  compare per-task rates over many seeds, not single runs.
- **A failed consult must fail the run.** pi carries on without advice, so a quota cut
  silently turns the advised arm into A0. `bench run` now fails such an attempt and stops on
  an advisor 401/403.

## Next

1. `H-case-back` on the 20 held-out `dev` tasks; if it holds, the full gate (25 tasks × 3
   seeds, paired with A0 and `L-best3`).
2. `H-case-stuck` on the 5 loop tasks once Kimi's weekly quota resets; then the stop: an
   acceptance check that reaches the hidden tests' context (user namespace, sibling types).
