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

1. Find where the help is lost: does Qwen act on correct advice (uptake per consult, advice
   quality judged against the upstream fix)? If the advice is right and ignored, the lever is
   the executor's side (how advice is injected, plan-following), not the brief.
2. Only then spend more advisor tokens: interface hints, an advisor-written acceptance test.
