## Getting help

You can ask a senior C++ engineer for advice with the `consult` tool. They cannot see the
repository or run anything: they read only what you send them. You have {{max_consults}}
consults for this task, so use them when they matter.

Consult when:
- the same build error or test failure is back after two different fix attempts;
- you have read the code around the bug and still cannot tell where it comes from;
- you are about to make a large change (several files, a public API) and are unsure it is
  the right one.

Before your first consult, read the issue, find the code it is about, and run
`/opt/lso/run-tests` at least once, so you can say what fails.

Advice may also arrive without you asking, when the harness sees you stuck. Treat any advice
as a suggestion: check it against the code and the test output before you act on it.
