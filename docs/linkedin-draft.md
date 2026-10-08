# LinkedIn draft (interim; numbers from docs/results.md)

Can a cloud model give a local coding agent a useful second opinion, when the code itself
must stay on your machine?

I've been testing this on 25 real C++ bugs from Multi-SWE-bench (fmt, nlohmann/json, Catch2).
A local Qwen 27B agent does the work; Kimi K3 in the cloud only gives advice, based on what the
agent chooses to tell it.

Before testing the advisor, I set a bar: three free local attempts and a local picker. Cloud
help has to beat that, not just a single run.

What I found so far:

1. Asking Kimi to pick the best of three local patches barely helped: 28 vs 25 out of 74.
   Even a perfect picker would only reach 38. Most wrong patches fail on an API the
   hidden tests expect, which no diff review can see.

2. Hints during the run lifted a single attempt from ~20 to 25 resolved out of 75, exactly
   matching the three-attempt bar. No gain.

3. Then I read what Kimi had actually been shown. It never saw a single command output, the
   agent's reasoning or its edits. It was guessing. Once the brief carried that evidence, its
   advice turned into real diagnoses: it caught a signed/unsigned bug, and pointed to the same
   fix the maintainers made upstream.

4. And yet: better diagnoses did not mean more fixes. With the evidence brief the agent
   resolved 19 of 60, against 23 for three free local attempts. The early runs that looked
   better (8 of 18) were noise.

So the bottleneck is not only what the advisor sees. Next I'm measuring whether the local
agent actually acts on advice that is right.

Lessons so far:
- Check what your advisor sees before tuning what it says.
- Single runs of a local agent are noisy. Compare many seeds per task.
- If a failed advice call doesn't fail the run, an outage quietly turns your "advised" agent
  back into the plain one.

The harness (Python + MLflow, with a TypeScript pi extension for the advisor) will be
open-source. More soon.

#LLM #AIAgents #SoftwareEngineering #Privacy #MLflow
