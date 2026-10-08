# Results (interim, 2026-10-08)

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
| `H-evidence`: same, richer brief (partial) | + command outputs, executor reasoning, edits | 8 / 18 | not yet |

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
- **`H-evidence`, partial:** 8 of 18 clean runs on 14 tasks, vs 6.8 expected for A0 and 6.0
  for `H-phase` on the same tasks (per-task rates). Too few runs to call; the run stopped when
  Kimi's weekly plan quota ran out.

## Cost and exposure

| Run | Consults | Kimi tokens in / out |
| --- | --- | --- |
| `review` on gate-a0 | 70 | 471k / 156k |
| gate-phase | 195 | 172k / 126k |
| gate-evidence (so far) | 54 | 124k / 47k |

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

1. Finish `H-evidence` (about 51 runs, ~130 consults) when the quota allows, then compute its
   gap closed with the task bootstrap.
2. If it is close to the gate: interface hints and an advisor-written acceptance test.
3. If it passes: redaction levels, then `--final` on `test`.
