## Getting help

You fix this task yourself: read the code, run `/opt/lso/run-tests`, find the cause, write the
fix. A senior C++ engineer can give a second opinion through the `consult` tool. They cannot
see the repository or run anything; they read only what you send. You have {{max_consults}}
consults.

Consult only after your own investigation, and only when:
- you have a concrete hypothesis and want it checked before a large or risky change; or
- you are stuck: the same failure is back after two different fixes, or you cannot find the
  cause in the code you have read.

Say what you tried and what you think the cause is, in under {{field_target_words}} words each.
A consult too soon after the last one, or without that, may be refused.

Advice can also arrive unasked. It is a second opinion, not the answer: check it against the
code and the test output, and keep doing the work yourself.
