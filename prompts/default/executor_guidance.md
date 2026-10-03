## Getting help

You fix this task yourself: read the code, run `/opt/lso/run-tests`, find the cause, write the
fix. A senior C++ engineer can give a second opinion through the `consult` tool. They cannot
see the repository or run anything; they read only what you send. You have {{max_consults}}
consults, and they are there to be used.

Consult at moments like these:
- after your first failing test run, if the cause is not obvious from the output;
- before a change that touches more than one file;
- when the same error comes back after a fix.

Say what you tried and what you think the cause is, in under {{field_target_words}} words each.

Advice can also arrive unasked. It is a second opinion, not the answer: check it against the
code and the test output, and keep doing the work yourself.
