File a case report with a senior C++ engineer. They see only what you write here and the
latest build or test output; no code from the repository is sent. You have {{max_consults}}
consults. Write in words and small made-up examples; do not paste the project's code.

Arguments, each under {{field_target_words}} words:
- tried: the case. Symptom class (compile, link, wrong output, crash/UB, error handling,
  missing API, regression, threading, system API, platform, build config, performance).
  How you reproduced it. Expected vs actual. Where in the code it happens (file, function).
  What you ruled out. On a follow-up: the experiment you ran and what it showed.
- hypothesis: what you think the cause is, why, and how sure you are.
- question: the one thing you need decided, e.g. "Is the bug in the parser or the
  serializer, given that ...?", not "How do I fix this?".
