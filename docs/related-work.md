# Related work

Prior work that bears on llm_second_opinion's design: a local executor (Qwen3.8 27B in pi)
consults a cloud advisor (Kimi K3) through redacted briefs, and the help policy is tuned by
GEPA-style reflective prompt search. Compiled 2026-10-03. Every citation below was checked
against its arXiv abstract page, repository, or vendor documentation on that date. Items
marked **[unverified]** could not be checked from the primary source; they are noted where
they appear.

## Summary: what matters most for this project

1. The closest prior system is a commercial product, not a paper: Anthropic's **advisor tool**
   (executor consults a stronger model mid-task). Its documentation reports timing results
   (consult early, after orientation and before the first edit; consult again before declaring
   done), a turn-2 nudge that helps weak executors and *hurts* when it fires before the
   executor has context, and gains that shrink as the executor approaches the advisor. Our
   `plan`, `stuck`, and `executor_guidance` designs should start from these findings.
2. That advisor sees the **full transcript**. Our redacted briefs are the new part. So the
   paper's contribution is the price of privacy (resolve rate lost per unit of exposure
   avoided), and we need an **L3 full-context** arm to measure that price.
3. **PAPILLON** is the closest academic precedent for privacy-conscious delegation: a local
   model writes the remote query, prompts are optimised against *quality − leakage*, and
   leakage is scored as the share of private units present in what was sent. GEPA's own paper
   uses PAPILLON's PUPA task as a benchmark. Reuse that framing and its leakage metric.
4. **Minions** shows the other design: the cloud model plans and the local model reads the
   data. That is a natural baseline ("advisor decomposes, executor executes").
5. Placeholder redaction is weak against inference. LLMs re-identify from context (Staab et
   al.; agentic re-identification work), and SWE-bench-style repositories are **memorised**
   (models name the buggy file from the issue text alone). Counting identifiers is not enough:
   add a **re-identification probe** (can the advisor name the repository, file, or function
   from the brief?).
6. GEPA's reported budgets are **thousands** of rollouts, mostly spent on validation. It
   needed only tens to hundreds of *training* rollouts to match GRPO. At our 100–300 budget,
   validation is the bottleneck: use a small, fixed, informative val set, cheap rejection on
   minibatches, and the `gepa` package's `val_evaluation_policy`, `objective_scores`, and
   `frontier_type` options.
7. **Meta-Harness** (Claude Code as proposer) found that giving the proposer **raw traces
   through a filesystem** beat scores alone and scores plus summaries by a wide margin. If
   Claude is the proposer, give it the item directories and use the digest only as an index,
   not as a replacement.
8. Binary resolve is noisy and sparse. Seed standard deviations of 0.5–3 pp on SWE-bench
   Verified are typical (SERA), and benchmark tests both pass wrong patches and fail right ones
   (PatchDiff, UTBoost, flaky tests). Use resolve as the score, and pass richer signals
   (per-test outcomes, build success, advice uptake) to the proposer as *feedback text*, not
   reward.
9. With 25 test tasks, a paired exact test has roughly **6% power for a +10 pp net effect and
   ~30% for +20 pp** (our calculation below). The test confirmation can only find large effects.
   Report intervals, pre-register the comparisons, and grow the test pool if possible.
10. Baselines reviewers will expect: A0 and A4 (have), **always consult at plan time** (the
    simplest fixed policy), **full-context advisor (L3, transcript-like)**, a Minions-style
    planner baseline, a hand-written prompt set without search (to show what search added),
    and cost-matched comparisons on a Pareto plot (HAL, "AI Agents That Matter").

## 1. Local–cloud collaboration and privacy-conscious delegation

**PAPILLON.** Li Siyan, Vethavikashini Chithrra Raghuram, Omar Khattab, Julia Hirschberg,
Zhou Yu. *PAPILLON: Privacy Preservation from Internet-based and Local Language Model
Ensembles.* 2024 (NAACL 2025). [arXiv:2410.17127](https://arxiv.org/abs/2410.17127).
A local model writes a privacy-preserving prompt for an API model, then combines the API's
answer with the original query. The pipeline's prompts are optimised with DSPy MIPROv2 on 150
examples, maximising (quality − leakage + well-formedness)/2. Quality is an LLM judge
("at least as good as the reference", both orderings); leakage is the percentage of annotated
PII units that appear in the prompt sent out. Result: 85.5% of queries keep their quality with
7.5% leakage (Llama-3.1-8B + GPT-4o-mini), on the new PUPA benchmark.
*For us:* the same problem with code in place of PII. Adopt its composite-objective idea and
its leakage definition ("share of private units that left"), applied to identifiers, file
paths, and code lines. A PAPILLON-style baseline is "the executor rewrites the brief itself"
rather than our rule-based L0–L3 redaction.

**Minions / MinionS.** Avanika Narayan, Dan Biderman, Sabri Eyuboglu, Avner May, Scott
Linderman, James Zou, Christopher Ré. *Minions: Cost-efficient Collaboration Between
On-device and Cloud Language Models.* 2025. [arXiv:2502.15964](https://arxiv.org/abs/2502.15964);
code at [HazyResearch/minions](https://github.com/HazyResearch/minions).
The cloud model never sees the documents. In Minion it chats with a local model; in MinionS
it writes subtasks that the local model runs in parallel over chunks. The naive protocol
cuts remote cost 30.4× but recovers 87% of cloud quality; MinionS cuts cost 5.7× and recovers
97.9%. Local models struggled with multi-step instructions and long contexts. Cost is modelled
as remote prefill plus weighted decode tokens, with local compute treated as free. The paper
says it does not address privacy; the repository adds an encrypted "secure" variant.
*For us:* the inverse role split (cloud plans, local reads) is a cheap baseline that fits our
abstraction story: a `plan` trigger with an advisor prompt that asks for a numbered plan of
local checks. Their cost model (local free, remote by tokens) matches our metering. Their
finding that small models fail on multi-step instructions argues for short, one-step advice.

**CodeCloak.** Amit Finkman Noah, Avishag Shapira, Eden Bar Kochva, Inbar Maimon, Dudu
Mimran, Yuval Elovici, Asaf Shabtai. *CodeCloak: A Method for Evaluating and Mitigating Code
Leakage by LLM Code Assistants.* 2024. [arXiv:2404.09066](https://arxiv.org/abs/2404.09066).
A deep RL agent rewrites prompts to a code assistant (deleting functions, renaming,
summarising code) to trade leakage against suggestion quality. The authors also build a
reconstruction method that estimates how much of a codebase can be rebuilt from the snippets
sent over a session.
*For us:* the **cumulative, session-level** view of leakage is the right one. Our exposure
should add up all briefs in a run (and across runs on the same repo), not score each brief
on its own. Their renaming and summarisation actions match our L0–L2.

**CodeCipher.** Yalan Lin, Chengcheng Wan, Yixiong Fang, Xiaodong Gu. *CodeCipher: Learning
to Obfuscate Source Code Against LLMs.* 2024. [arXiv:2410.05797](https://arxiv.org/abs/2410.05797).
Token-level obfuscation through a learned remapping of the embedding matrix; it needs access
to the model's weights. *For us:* not applicable to a closed API advisor. Cite it to show
the design space.

**Robustness and trade-offs on protected code.** Jin Wen, Yuejun Guo, Yujie Ma, Qiang Hu,
Maxime Cordy. *Robustness and Trade-offs for Code LLMs on Protected Code.* 2026.
[arXiv:2609.04220](https://arxiv.org/abs/2609.04220).
Seven code LLMs, five obfuscation methods, C++/Go/Java/JavaScript. Strong models (GPT-4.1,
Qwen3-Coder-30B) keep about 90% Pass@1 on obfuscated code translation, and direct inference on
obfuscated code often matches deobfuscated code. They recommend execution-based metrics.
*For us:* expect frontier advisors to tolerate identifier redaction well. The cost of L2 versus
L3 may be small, which is itself a finding worth reporting.

**Beyond memorization.** Robin Staab, Mark Vero, Mislav Balunović, Martin Vechev. *Beyond
Memorization: Violating Privacy Via Inference with Large Language Models.* 2023 (ICLR 2024).
[arXiv:2310.07298](https://arxiv.org/abs/2310.07298).
LLMs infer personal attributes from text with up to 85% top-1 accuracy, and text
anonymisation does not stop them. *For us:* removing names does not remove information. The
advisor may infer the project from the bug's behaviour.

**Agentic re-identification.** Tianshi Li. *Agentic LLMs as Powerful Deanonymizers:
Re-identification of Participants in the Anthropic Interviewer Dataset.* 2026.
[arXiv:2601.05918](https://arxiv.org/abs/2601.05918). Off-the-shelf agents with web search
linked 6 of 24 redacted interviews to specific papers and authors. Related defences:
*LLM Anonymization Against Agentic Re-Identification* (AURA), 2026,
[arXiv:2605.30848](https://arxiv.org/abs/2605.30848) (details from the search snippet only,
**[abstract not opened]**). Also Sherwin Vishesh Jathanna, *SurrogateShield: Beyond Redaction
for High-Utility, Privacy-Preserving LLM Interactions*, 2026,
[arXiv:2606.29567](https://arxiv.org/abs/2606.29567): type-consistent surrogate values in place of
placeholders raise BERTScore from 81.6% to 94.9%, and a prompted adversary recovered no
originals in 100 trials.
*For us:* (a) measure exposure with an **attack**, not just a count: give a separate model the
briefs of a run and ask it to name the repository, file, and function. (b) Try **surrogate
names** (plausible fake identifiers) as an alternative to `<function_1>` placeholders. They may
help the advisor's reasoning more than opaque placeholders do.

**SWE-bench memorisation (also relevant to exposure).** Shanchao Liang, Spandan Garg, Roshanak
Zilouchian Moghaddam. *The SWE-Bench Illusion: When State-of-the-Art LLMs Remember Instead of
Reason.* 2025. [arXiv:2506.12286](https://arxiv.org/abs/2506.12286). Models name the buggy file
path from the issue text alone up to 76% of the time on SWE-bench, but 53% on other
repositories. *For us:* nlohmann/json, fmt, simdjson, and Catch2 are among the most-starred C++
repositories, so the advisor probably knows them. At L0 the "abstracted" issue may still let
it recall the real code, which would overstate both L0 utility and L0 privacy. The
re-identification probe above measures this. Report it per level.

## 2. Advisor, consultant, and "ask for help" patterns

**Anthropic advisor tool.** *The advisor strategy* (blog, 2026-04-09,
[claude.com/blog/the-advisor-strategy](https://claude.com/blog/the-advisor-strategy)) and the
[advisor tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)
(`advisor_20260301`, beta). The executor calls a parameterless `advisor` tool. The server
passes the advisor the **executor's full transcript** (system prompt, tools, all turns and tool
results), and the advisor runs without tools. `max_uses` caps calls per request. The blog
reports Sonnet + Opus advisor at +2.7 pp on SWE-bench Multilingual with 11.9% lower cost per
task, and Haiku going from 19.7% to 41.2% on BrowseComp. The docs give coding guidance:
- call once early, "after a few exploratory reads", before the first edit;
- call again before declaring done, and when stuck or about to change approach;
- a suggested prompt section on how to treat advice, which says to weigh it seriously and to
  ask a reconcile question when the evidence conflicts;
- a Haiku "hard rule" (consult before the first state-changing command) that gave about
  +7.5 pp on an internal coding benchmark but −4 pp on browsing;
- a turn-2 nudge that gave Haiku about +7 pp, did nothing for Sonnet, and slightly hurt Opus.
  When an executor's own first call came at turn 7 or later, a turn-2 nudge cost 3–4 pp.

The companion page,
[Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence),
reports that gains shrink as the executor nears the advisor. It also says "an executor at low
effort can stop detecting that it is stuck", so consult rates collapse and scores can fall
*below the executor alone*.
*For us:* this is the strongest prior evidence on *when to ask*. (a) Our `plan` trigger fires
before turn 0, with no context. Anthropic's data say a too-early consult can displace a better
one, so add a "plan after orientation" variant (first consult after N read-only turns or before
the first edit). (b) Add a "before done" trigger, fired when the executor ends a turn without a
tool call or runs the tests the final time. (c) Our executor is a local 27B model that may
under-detect being stuck, which supports harness triggers. (d) Their advice-treatment text is a
ready seed for `advice_injection` and `executor_guidance`. (e) The difference: their advisor
sees everything; ours sees a brief.

**Advisor Models.** Parth Asawa, Alan Zhu, Abigail O'Neill, Matei Zaharia, Alexandros G.
Dimakis, Joseph E. Gonzalez. *How to Train Your Advisor: Steering Black-Box LLMs with Advisor
Models.* 2025. [arXiv:2510.02453](https://arxiv.org/abs/2510.02453). The roles are the reverse
of ours: a small open model is trained with RL to write per-instance advice for a black-box
frontier model. It improves GPT-5.2 on RuleArena (Taxes) by 27.4% and cuts Gemini 3 Pro's
steps on SWE agent tasks by 24.6%. *For us:* advice is a learnable, per-instance object. A later
step past prompt search is to train the *brief writer* (the executor's side) with RL against
resolve − exposure.

**Steer, Don't Solve.** Shubham Gandhi, Yiqing Xie, Atharva Naik, Ruichen Zhu, Carolyn Rose.
*Steer, Don't Solve: Training Small Critic Models for Large Code Agents.* 2026.
[arXiv:2606.21811](https://arxiv.org/abs/2606.21811). Small (4B/8B) critics trained to give
"high-level guidance without generating concrete actions" improve six code agents (e.g. +16.0%
for GLM-4.7-Flash on SWE-bench Verified) and lower cost. They name planning as the main
bottleneck. *For us:* evidence that hint-style advice, without code, can carry most of the
value. That supports keeping `hints-only` as a main variant, and it favours lower levels for
exposure.

**SWE-PRM.** Shubham Gandhi, Jason Tsay, Jatin Ganhotra, Kiran Kate, Yara Rizk. *When Agents go
Astray: Course-Correcting SWE Agents with PRMs.* 2025.
[arXiv:2509.02360](https://arxiv.org/abs/2509.02360). An inference-time process reward model
watches the trajectory, flags failure patterns from a taxonomy (redundant exploration, loops,
premature termination), and injects corrections. Resolve on SWE-bench Verified goes from
40.0% to 50.6% with closed-source PRMs, for as little as $0.2 more per task. *For us:* this is
a "harness-triggered advisor" with a taxonomy. Their failure taxonomy is a good source for
stuck-trigger conditions and for the questions a trigger asks.

**Aider architect/editor mode.** Aider blog, *Separating code reasoning and editing*,
2024-09-26, [aider.chat/2024/09/26/architect.html](https://aider.chat/2024/09/26/architect.html).
A strong "architect" describes the change and a second "editor" model writes the edit;
o1-preview + DeepSeek/o1-mini reached 85% on aider's editing benchmark. *For us:* a
plan-then-edit split helps even between strong models. Advice as "what to change and why",
with the executor writing the edit, is a proven division of labour.

**Amp oracle.** Amp (formerly Sourcegraph) docs, [ampcode.com/docs/tools](https://ampcode.com/docs/tools).
The main agent has an `oracle` tool backed by a different frontier model at high reasoning,
used "when debugging or reviewing a complex piece of code". It is deliberately not forced on
every task because of cost and latency. Which model backs it depends on the mode and changes
over time. *For us:* the product version of our executor-initiated `consult`. No published
evaluation.

**Cascades and routing.** Lingjiao Chen, Matei Zaharia, James Zou, *FrugalGPT*, 2023,
[arXiv:2305.05176](https://arxiv.org/abs/2305.05176): cascades match GPT-4 at up to 98% lower
cost. Isaac Ong et al., *RouteLLM: Learning to Route LLMs with Preference Data*, 2024,
[arXiv:2406.18665](https://arxiv.org/abs/2406.18665): learned strong/weak routers cut cost more
than 2× without quality loss. Pranjal Aggarwal et al., *AutoMix: Automatically Mixing Language
Models*, 2023 (NeurIPS 2024), [arXiv:2310.12963](https://arxiv.org/abs/2310.12963): small-model
self-verification plus a POMDP router cut cost by over 50%. *For us:* these route a whole
query, once. We escalate mid-trajectory and partially. They give the vocabulary
(cost–quality curve, escalation threshold) and a baseline: **route the whole task** to A4 when
a cheap signal (e.g. A0 fails its first build) says so. That baseline is a cost-matched rival to
consulting.

**Mid-trajectory deferral.** Dzianis Piatrashyn et al., *ReDAct: Uncertainty-Aware Deferral for
LLM Agents*, 2026, [arXiv:2604.07036](https://arxiv.org/abs/2604.07036): deferring about 15% of
step decisions to the large model, when small-model uncertainty passes a calibrated threshold,
matches the large model's quality (ALFWorld, MiniGrid). Nadeem Shaikh, *Knowing When to Ask
for Help: Bayesian Self-Escalation in Hierarchical LLM Agents*, 2026,
[arXiv:2608.24087](https://arxiv.org/abs/2608.24087): escalation as optimal stopping. On a
1.5B→7B MBPP cascade it beats post-hoc routing at equal cost. *For us:* token-level uncertainty
from the local model (logprobs from the MLX server) is a candidate fifth trigger. Both papers
use small, simple tasks; nothing comparable exists for repository-level C++ repair.

**Do agents know when to ask?** Tu Trinh et al., *HiL-Bench (Human-in-Loop Benchmark): Do
Agents Know When to Ask for Help?*, 2026, [arXiv:2604.09408](https://arxiv.org/abs/2604.09408).
Frontier coding agents under-escalate when specs are incomplete. The Ask-F1 metric (precision
of questions × recall of real blockers) is trainable with RL. *For us:* an "ask-precision"
analysis of executor-initiated consults: were they on items A0 fails? This is the right way to
grade `consult_tool` and `executor_guidance` variants beyond resolve rate.

**Stuck detection.** OpenHands *Stuck Detector*
([docs](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)): same action and
observation 4 or more times, same action and error 3 or more times, 3 or more monologue
messages, and an A–B–A–B alternation over 6 or more cycles; events are compared on content.
*For us:* our `stuck` heuristic covers repeats, same error, and no-diff turns. Add the
**alternation** pattern, and use their thresholds as defaults so they are not tuned on dev.

## 3. Prompt and agent optimisation

**GEPA.** Lakshya A Agrawal, Shangyin Tan, Dilara Soylu, Noah Ziems, Rishi Khare, Krista
Opsahl-Ong, Arnav Singhvi, Herumb Shandilya, Michael J Ryan, Meng Jiang, Christopher Potts,
Koushik Sen, Alexandros G. Dimakis, Ion Stoica, Dan Klein, Matei Zaharia, Omar Khattab. *GEPA:
Reflective Prompt Evolution Can Outperform Reinforcement Learning.* 2025 (revised 2026).
[arXiv:2507.19457](https://arxiv.org/abs/2507.19457). Beats GRPO by 6% on average (up to 20%)
with up to 35× fewer rollouts, and beats MIPROv2 by more than 10%. Its prompts are up to 9.2×
shorter than MIPROv2's and show a smaller generalisation gap. Total budgets were 1,839–7,051
rollouts per task. "The majority of GEPA's rollout budget is spent on validation". Counting only
training rollouts, it needed 79–737 to reach its best, and as few as 6–179 to match GRPO. The
Pareto pool keeps any candidate that is best on at least one validation task, and merging
lineages added up to 5%. PUPA (PAPILLON) is one of its six tasks.
*For us:* the method fits multi-slot prompts with rich traces. The budget is the catch: our 300
rollouts are about 5–15% of theirs. Learning is cheap; validation is expensive.

**`gepa` package.** [github.com/gepa-ai/gepa](https://github.com/gepa-ai/gepa), checked in
`src/gepa/core/adapter.py` and `src/gepa/api.py`.
- `GEPAAdapter.evaluate(batch, candidate: dict[str,str], capture_traces) -> EvaluationBatch`,
  where `EvaluationBatch` has `outputs`, `scores`, optional `trajectories`, `objective_scores`
  (a per-example dict of objectives), and `num_metric_calls`.
- `make_reflective_dataset(candidate, eval_batch, components_to_update)` returns per-component
  JSON-serialisable records.
- `propose_new_texts` is optional and overrides the proposer.
- `optimize(...)` takes `max_metric_calls`, `reflection_minibatch_size` (3 by default with
  epoch-shuffled batches), and `module_selector` (`round_robin` or `all`).
- `frontier_type` is `instance`, `objective`, `hybrid`, or `cartesian`;
  `candidate_selection_strategy` is `pareto`, `current_best`, `epsilon_greedy`, or
  `top_k_pareto`.
- Further options: `val_evaluation_policy`, `acceptance_criterion`, `skip_perfect_score=True`,
  `perfect_score`, `use_merge`, `max_reflection_cost`, `run_dir`, `use_mlflow`, `seed`.
- There is also an `optimize_anything` entry point.

*For us:* the adapter maps one-to-one onto Runner plus item directories, and a candidate is
exactly our slot→text dict. Three settings matter:
- `objective_scores` with `frontier_type="hybrid"` lets cost and exposure enter the Pareto pool
  without collapsing them into one scalar.
- `skip_perfect_score` skips reflection when every minibatch item scores 1. With binary
  resolve and minibatches of 3–4, that happens often on easy tasks; keep minibatches on
  discriminating tasks, as the spec plans.
- `val_evaluation_policy` allows partial validation, which saves budget.

**DSPy MIPROv2.** Krista Opsahl-Ong, Michael J Ryan, Josh Purtell, David Broman, Christopher
Potts, Matei Zaharia, Omar Khattab. *Optimizing Instructions and Demonstrations for
Multi-Stage Language Model Programs.* 2024 (EMNLP 2024).
[arXiv:2406.11695](https://arxiv.org/abs/2406.11695). Bayesian search over proposed
instructions and demos per module, up to +13% on Llama-3-8B pipelines. *For us:* it relies on
many cheap trials (PAPILLON used 200 trials on 150 examples) and on few-shot demos, which we
cannot use: our "examples" are 20-minute agent runs. Not a fit.

**DSPy SIMBA.** [DSPy docs](https://dspy.ai/current/api/optimizers/SIMBA/) (no paper). It
samples minibatches (32 by default), finds examples with high score variance across samples,
and writes self-reflective rules or adds demos (8 steps, 6 candidates by default). *For us:*
the idea of focusing on **high-variance examples** is useful. Tasks that a policy solves on
some seeds and not others are where prompt changes show up. The default budget (about 32 × 6
× 8 evaluations) is far beyond ours.

**TextGrad** (Mert Yuksekgonul et al., 2024, [arXiv:2406.07496](https://arxiv.org/abs/2406.07496)),
**Trace/OptoPrime** (Ching-An Cheng, Allen Nie, Adith Swaminathan, 2024,
[arXiv:2406.16218](https://arxiv.org/abs/2406.16218)), **OPRO** (Chengrun Yang et al., 2023,
ICLR 2024, [arXiv:2309.03409](https://arxiv.org/abs/2309.03409)), **Promptbreeder**
(Chrisantha Fernando et al., 2023, [arXiv:2309.16797](https://arxiv.org/abs/2309.16797)), and
**EvoPrompt** (Qingyan Guo et al., 2023, ICLR 2024,
[arXiv:2309.08532](https://arxiv.org/abs/2309.08532)).
- TextGrad and Trace turn textual critiques of execution traces into edits, as GEPA's
  reflection step does. Trace's "the execution trace is the gradient" is the same idea as our
  reflective dataset.
- OPRO, Promptbreeder, and EvoPrompt are population or score-history methods that need
  hundreds to thousands of cheap evaluations on short tasks.

*For us:* none adds anything GEPA lacks for this setting. Score-only methods (OPRO, EvoPrompt)
waste our most valuable asset, the trace.

**ACE.** Qizheng Zhang, Changran Hu, Shubhangi Upasani, Boyuan Ma, Fenglu Hong, Vamsidhar
Kamanuru, Jay Rainton, Chen Wu, Mengmeng Ji, Hanchen Li, Urmish Thakker, James Zou, Kunle
Olukotun. *Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models.*
2025 (ICLR 2026). [arXiv:2510.04618](https://arxiv.org/abs/2510.04618). Contexts grow as
"playbooks" through generate → reflect → curate, with incremental delta edits that avoid
"brevity bias" and "context collapse" (rewrites that erase detail). +10.6% on agent tasks
(AppWorld). *For us:* whole-slot rewrites by a proposer risk context collapse. Prefer
**append or edit** mutations over full rewrites for `executor_guidance`, and track slot length.

**Meta-Harness.** Yoonho Lee, Roshen Nair, Qizheng Zhang, Kangwook Lee, Omar Khattab, Chelsea
Finn. *Meta-Harness: End-to-End Optimization of Model Harnesses.* 2026.
[arXiv:2603.28052](https://arxiv.org/abs/2603.28052). The proposer is Claude Code (Opus 4.6),
reading all prior candidates' code, scores, and raw traces from a filesystem. It beat
hand-engineered harnesses on TerminalBench-2. In the ablation, scores only gave 34.6 median /
41.3 best; scores plus summaries 34.9 / 38.7; full trace access 50.0 / 56.7. "Summaries may even
hurt by compressing away diagnostically useful details." One search was 40 candidates.
*For us:* strong support for "Claude as interactive proposer", **as long as it reads the raw
item directories** (`events.jsonl`, `advice.jsonl`, `grade.log`). A digest is fine as a table
of contents, not as the only input.

**Harness self-improvement with overfitting control.** Peng Xia et al., *RRSI: Regularized
Recursive Self-Improvement of Agent Harnesses*, 2026,
[arXiv:2609.24972](https://arxiv.org/abs/2609.24972): annealed edit budgets, a critic that
rejects benchmark-specific proposals, and a pruner of costly changes. Up to +14.1 on training
benchmarks but only +4.7 out of distribution, with 30% fewer tokens. Sungho Park et al.,
*AutoSaddler*, 2026, [arXiv:2608.23041](https://arxiv.org/abs/2608.23041): failure-trace
diagnosis, structured patches, and validation-based selection; +9.6 pp on SWE-Bench Pro.
*For us:* the train vs. out-of-distribution gap (14 vs. 5) is the size of overfitting to
expect. Their fixes are cheap to copy: tell the proposer not to mention task-specific facts,
and reject candidates whose text names repositories, files, or functions.

**ADAS, Darwin Gödel Machine, AlphaEvolve.** Shengran Hu, Cong Lu, Jeff Clune, *Automated
Design of Agentic Systems*, 2024, [arXiv:2408.08435](https://arxiv.org/abs/2408.08435). Jenny
Zhang, Shengran Hu, Cong Lu, Robert Lange, Jeff Clune, *Darwin Godel Machine: Open-Ended
Evolution of Self-Improving Agents*, 2025, [arXiv:2505.22954](https://arxiv.org/abs/2505.22954):
SWE-bench 20.0%→50.0%, over 80 iterations and about two weeks per run, with **staged
evaluation** (10 tasks, then 50, then 200 for the top candidates). Alexander Novikov et al.,
*AlphaEvolve*, 2025, [arXiv:2506.13131](https://arxiv.org/abs/2506.13131). *For us:* code-level
scaffold search is out of budget. The reusable idea is DGM's **staged evaluation**: screen a
candidate on a few discriminating tasks and promote only survivors to full validation.

**Budget-aware selection.** Lennart Schneider et al., *Hyperband-based Bayesian Optimization for
Black-box Prompt Selection*, 2024 (ICML 2025), [arXiv:2412.07820](https://arxiv.org/abs/2412.07820):
multi-fidelity (successive-halving) evaluation cuts the validation instances needed. *For us:*
the formal version of staged evaluation.

**Which fits?** For five slots, 10–40 minute noisy rollouts, and 100–300 rollouts: GEPA-style
reflection (one slot at a time, rich traces) with staged or successive-halving validation, and
a Meta-Harness-style proposer with raw-trace access. MIPROv2, SIMBA, OPRO, EvoPrompt, and
Promptbreeder need one or two orders of magnitude more evaluations. ADAS and DGM search code
rather than prompts and cost weeks. Expect gains of a few points, mostly on dev, with about a
third of that surviving out of distribution (RRSI's ratio).

## 4. Reward and evaluation signals

**Execution-based resolve, and its errors.** You Wang, Michael Pradel, Zhongxin Liu, *Are
"Solved Issues" in SWE-bench Really Solved Correctly? An Empirical Study*, 2025,
[arXiv:2503.15223](https://arxiv.org/abs/2503.15223): 7.8% of "correct" patches fail the
developer tests; 29.6% of plausible patches behave differently from the reference (PatchDiff),
inflating resolve by about 6 pp. Boxi Yu, Yuxuan Zhu, Pinjia He, Daniel Kang, *UTBoost:
Rigorous Evaluation of Coding Agents on SWE-Bench*, 2025 (ACL 2025),
[arXiv:2506.09289](https://arxiv.org/abs/2506.09289): extra generated tests caught 345
wrongly passing patches and changed leaderboard rankings. *For us:* for the reported winners,
check behaviour against the reference patch (a PatchDiff-style differential test, or a manual
read). Prompt search optimises the grader as much as the task.

**Verifiers and execution-free rewards.** Jiayi Pan et al., *Training Software Engineering
Agents and Verifiers with SWE-Gym*, 2024 (ICML 2025),
[arXiv:2412.21139](https://arxiv.org/abs/2412.21139): trajectory verifiers plus best-of-n reach
32.0% on SWE-bench Verified. Naman Jain et al., *R2E-Gym: Procedural Environments and Hybrid
Verifiers for Scaling Open-Weights SWE Agents*, 2025,
[arXiv:2504.07164](https://arxiv.org/abs/2504.07164): execution-based and execution-free
verifiers each plateau at about 42–43%, and combined reach 51%. Execution-free ones are biased
toward style. Yuxiang Wei et al., *SWE-RL*, 2025 (NeurIPS 2025),
[arXiv:2502.18449](https://arxiv.org/abs/2502.18449): a patch-similarity reward against the
ground truth drives RL to 41.0%. Chunqiu Steven Xia et al., *Agentless*, 2024,
[arXiv:2407.01489](https://arxiv.org/abs/2407.01489): localise → repair → validate with
reproduction tests and reranking, 32% on Lite at $0.70.
*For us:* we have execution, so a learned verifier is not needed as a reward. **Patch
similarity to the gold patch** (SWE-RL) and **fraction of fail-to-pass tests passing** are
cheap, dense secondary signals. They break ties among the many candidates that score 0 on a
minibatch, but they are gameable, so use them as tie-breakers and proposer feedback, never as
the acceptance score.

**Process rewards and judges.** SWE-PRM (above). Mingchen Zhuge et al., *Agent-as-a-Judge:
Evaluate Agents with Agents*, 2024, [arXiv:2410.10934](https://arxiv.org/abs/2410.10934):
agentic judges agree with humans better than plain LLM judges on DevAI. *For us:* an LLM judge
of **advice quality** (correct? actionable? did the executor follow it?) helps diagnosis and
reflection. Validate it on a hand-labelled sample before trusting it, and keep it out of the
acceptance rule.

**Cost and Pareto reporting.** Sayash Kapoor, Benedikt Stroebl, Zachary S. Siegel, Nitya
Nadgir, Arvind Narayanan, *AI Agents That Matter*, 2024,
[arXiv:2407.01502](https://arxiv.org/abs/2407.01502): optimise cost and accuracy jointly, and use
proper holdouts. Sayash Kapoor, Benedikt Stroebl, Peter Kirgis et al., *Holistic Agent
Leaderboard*, 2025, [arXiv:2510.11977](https://arxiv.org/abs/2510.11977): cost–accuracy Pareto
fronts over 21,730 rollouts; costly models are rarely Pareto-optimal; more reasoning effort often
*lowered* accuracy; LLM-aided log inspection found agents gaming tasks. *For us:* our Pareto
report is the expected form. Add **simple baselines on the same front** (A0 retries, A4 at low
effort, whole-task routing) and an LLM pass over logs to look for shortcut behaviour.

**Statistics on small task sets.**
- Evan Miller, *Adding Error Bars to Evals*, 2024,
  [arXiv:2411.00640](https://arxiv.org/abs/2411.00640): paired differences, clustered standard
  errors, and power analysis before running.
- Lovish Madaan et al., *Quantifying Variance in Evaluation Benchmarks*, 2024,
  [arXiv:2406.10229](https://arxiv.org/abs/2406.10229): seed variance is large relative to
  reported gaps.
- Ethan Shen, Daniel Tormoen, Saurabh Shah, Ali Farhadi, Tim Dettmers, *SERA: Soft-Verified
  Efficient Repository Agents*, 2026, [arXiv:2601.20789](https://arxiv.org/abs/2601.20789),
  Section 6: across 78 conditions × 3 seeds on SWE-bench Verified, seed standard deviation is
  0.5–3.0% (median 1.2%). "With only 3 seeds, improvements below 2–3% should be treated with
  skepticism".
- Shunyu Yao, Noah Shinn, Pedram Razavi, Karthik Narasimhan, *τ-bench*, 2024,
  [arXiv:2406.12045](https://arxiv.org/abs/2406.12045): the pass^k reliability metric.
- Franck Ndzomga, *Efficient Benchmarking of AI Agents*, 2026,
  [arXiv:2603.23749](https://arxiv.org/abs/2603.23749): keeping only tasks with 30–70%
  historical pass rates preserves rankings with 44–70% fewer tasks.

*Our own power calculation* (exact two-sided McNemar, α = 0.05, one seed per task, arm better
on 15% of tasks and worse on 5%, a +10 pp net effect):

| Tasks | +10 pp (15/5) | +15 pp (20/5) | +20 pp (25/5) |
| --- | --- | --- | --- |
| 25 | 0.06 | 0.17 | 0.30 |
| 49 | 0.23 | 0.47 | 0.69 |
| 100 | 0.54 | 0.84 | 0.96 |

Seeds reduce within-task noise but do not add tasks. The task-clustered bootstrap (already in
the spec) is the right interval. *For us:* (a) the 25-task test confirms only large effects, so
plan to report intervals and the share of the gap closed rather than "significant" winners, or
grow the test pool. (b) The 30–70% filter backs the spec's choice to build minibatches from
tasks where A0 fails and A4 succeeds. (c) Report pass^k (all seeds resolved) next to the mean,
since advice may make the executor more consistent rather than more capable.

## 5. Benchmarks and pitfalls

**Multi-SWE-bench.** Daoguang Zan et al., *Multi-SWE-bench: A Multilingual Benchmark for Issue
Resolving*, 2025, [arXiv:2504.02605](https://arxiv.org/abs/2504.02605); mini at
[HF ByteDance-Seed/Multi-SWE-bench_mini](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench_mini)
(400 instances, 50 per language; C++ from Catch2, fmt, nlohmann/json, simdjson, cpp-httplib).
The full set has 1,632 instances in 7 languages, annotated by 68 experts. C and C++ were the
hardest languages in the paper (e.g. C++ 3.10% vs. Python 44.60% for Claude-3.7-Sonnet with
MagentLess). Issues longer than 600 tokens resolve about 50% less often than ones under 200, and
multi-file fixes are much harder. Validation ran the suite as run / test-only / fix logs. *For
us:* tag each task with issue length and patch size (files, hunks), and report lift by
stratum. Advice may help most on long or multi-file issues. If we need more test tasks, the
full benchmark has more C++ instances for the same repositories than mini (per-repository
counts **[unverified]**: the figures extracted from the paper's table did not add up).

**Contamination and quality.**
- Reem Aleithan et al., *SWE-Bench+*, 2024, [arXiv:2410.06992](https://arxiv.org/abs/2410.06992):
  32.67% of successful patches had the solution in the issue or comments, and 31.08% passed
  because tests were weak.
- The SWE-Bench Illusion (above): memorisation.
- Ibragim Badertdinov et al., *SWE-rebench*, 2025 (NeurIPS 2025),
  [arXiv:2505.20411](https://arxiv.org/abs/2505.20411): fresh tasks expose inflated scores.
- OpenAI's *Introducing SWE-bench Verified* (2024-08-13, 500 human-screened tasks) and *Why
  SWE-bench Verified no longer measures frontier coding capabilities*: both pages returned
  HTTP 403; date and size are from search snippets only **[unverified]**.

*For us:* check each task's issue for solution leakage (a patch or exact fix in the text). At L3
the advisor sees it verbatim; at L0 redaction may remove it, which confounds level with
leakage. Note the task creation dates relative to both models' training cutoffs.

**Flaky tests.** Bradley Brown et al., *Large Language Monkeys*, 2024,
[arXiv:2407.21787](https://arxiv.org/abs/2407.21787), and its samples repository
([ScalingIntelligence/swe-bench-lite-samples](https://github.com/ScalingIntelligence/swe-bench-lite-samples)):
30 of 300 SWE-bench Lite instances (10%) had flaky tests that marked correct solutions wrong,
plus 4 with nondeterministic tests. They report both the full set and a 266-task subset.
*For us:* our "gold passes twice" rule is good. Also re-run the **A0 and candidate patches**
for any task whose result flips between seeds, to separate grader flakiness from policy
variance.

**What reviewers expect.** Yuxuan Zhu et al., *Establishing Best Practices for Building Rigorous
Agentic Benchmarks* (Agentic Benchmark Checklist, ABC), 2025,
[arXiv:2507.02825](https://arxiv.org/abs/2507.02825): task and grader flaws can distort results
by up to 100% relative. This and the HAL and "AI Agents That Matter" papers add up to a
checklist:
- held-out test used once;
- the number of configurations tried;
- seeds and intervals;
- cost on the same plot as accuracy;
- simple and strong baselines;
- manual or differential checks of "resolved" patches;
- contamination discussion;
- released logs.

The spec already covers most of this. The gaps are the baselines and the patch-correctness
audit.

## Recommendations

**Prompt search method**
1. Adopt GEPA through the `gepa` package with a custom adapter. Use `module_selector="round_robin"`,
   `reflection_minibatch_size` 3–4, `objective_scores={resolve, -cost, -exposure}` with
   `frontier_type="hybrid"`, and `acceptance_criterion="strict_improvement"` on minibatches.
   Set `skip_perfect_score` on purpose. Take the rollout budget from the pilot.
2. Spend validation like DGM and Hyperband: a new candidate first runs on a 3–4 task minibatch
   of discriminating tasks (A0 fails, A4 solves, or results vary across seeds). Only those that
   beat their parent go to the 8-task val set. Fix the val set and its seeds once.
3. If Claude is the proposer (Meta-Harness evidence favours this), give it the **raw item
   directories** plus the digest as an index, and log each proposal's reasoning. Keep an API
   proposer as the reproducible option and run both once to compare.
4. Prefer edit or append mutations over whole-slot rewrites (ACE context collapse). Cap slot
   length.
5. Add RRSI-style guards: tell the proposer to write task-agnostic rules, and automatically
   reject candidates whose text contains repository, file, or function names from dev tasks.
6. Seed the search with a prompt set based on Anthropic's published advisor guidance (timing
   block plus advice-treatment block, adapted to a brief-based advisor). It is a strong
   hand-written start and a baseline for what search adds.

**Reward design**

7. Score for acceptance is binary resolve, averaged over seeds. Pass the proposer per-test
   outcomes, build success, the fraction of fail-to-pass tests passing, patch similarity to
   gold, advice uptake, and an LLM-judge note on advice quality, all as feedback text. None of
   these is the score.
8. Validate any LLM judge of advice quality against about 30 hand-labelled consults before
   using it, even for diagnosis.
9. Audit the final winners' resolved patches with a PatchDiff-style differential check against
   the gold patch, or a manual read.

**Statistics**

10. Keep the task-clustered paired bootstrap as the primary measure. Pre-register the 2–3
    confirmatory comparisons (e.g. best H vs. A0, best H vs. always-plan) and correct for them.
11. State the power limits: with 25 test tasks only effects of about 20 pp or more are likely
    to be detected. Consider enlarging `test` from the full Multi-SWE-bench C++ pool (same
    repositories, same pipeline) before the final run, and use at least 3 seeds per item
    (SERA).
12. Report pass^k (all seeds resolved), lift by stratum (issue length, files touched), and the
    number of configurations tried (already planned).

**Exposure metrics**

13. Keep tokens and identifiers sent, and add:
    - a cumulative per-run total;
    - **leaked-unit share** (PAPILLON-style: the fraction of the gold patch's identifiers and
      touched file names that appear verbatim in any brief);
    - a **re-identification probe**: a separate model gets a run's briefs and must name the
      repository, file, and function; report top-1 and top-3 accuracy per level.
14. Report the probe on the issue text alone as a memorisation floor: if the advisor names the
    repository from L0, L0's privacy is partly nominal for these famous repositories.
15. Try surrogate identifiers (plausible fake names) as an L1/L2 variant, against `<function_1>`
    placeholders (SurrogateShield).

**Triggers and approach**

16. Add a **"plan after orientation"** trigger (first consult after k read-only turns or before
    the first edit) next to the turn-0 `plan`. Anthropic found too-early consults can displace
    better ones.
17. Add a **"before done"** trigger (the executor's final turn or last test run) and the
    OpenHands **alternation** pattern to `stuck`. Take stuck thresholds from OpenHands
    defaults rather than tuning them on dev.
18. Measure **ask precision** for executor-initiated consults (HiL-Bench): how many fall on
    items A0 fails.

**Baselines we must include**

19. **Always consult at plan time** (single consult, fixed prompt), the simplest policy.
20. **Full-context advisor** (L3 with the transcript or a long trace summary, Anthropic
    advisor-tool style), so the cost of redaction is measured.
21. **Minions-style planner**: the advisor gets only the abstracted issue and returns a
    numbered plan of local checks, with no further consults.
22. **PAPILLON-style brief**: the executor writes the brief itself under a privacy
    instruction, against our rule-based L0–L3 redaction.
23. **Whole-task routing / cascade**: run A0, and send the task to A4 only if A0 fails. This is
    a cost-matched rival on the Pareto front. Also an A0 retry (best of 2) at equal wall-clock
    cost.
24. The hand-written seed prompt sets on `test`, so the gain from search is shown, not
    assumed.

**Avoid**

25. MIPROv2, SIMBA, OPRO, EvoPrompt, or Promptbreeder at this budget. Code-level scaffold
    search (ADAS, DGM) in v1.
26. Using a dense proxy (patch similarity, judge scores) as the acceptance criterion.
27. Calling L0 "private" on the strength of identifier counts alone.
28. Reporting dev-set numbers or the best-of-many val score as results.
