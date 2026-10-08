## Investigate, report, then fix

You fix this task yourself. A senior C++ engineer advises through the `consult` tool. They
cannot see the repository or run anything, and no code is sent to them: they read only your
report and the latest build or test output. You have {{max_consults}} consults.

1. Investigate first. Reproduce the problem: write a small test or program, or run
   `/opt/lso/run-tests`, and note exactly what you see. Find where it happens in the code.
2. Classify the symptom: compile error, link error, wrong output, crash / assert / undefined
   behaviour, error handling, missing feature or API, regression, threading, system API / OS,
   platform or compiler, build configuration, performance.
3. File your report with `consult` before your first edit. Edits are refused until you have.
   Describe the behaviour in words and with small made-up examples, not by pasting the
   project's code.
4. Act on the answer: run the experiment it ranks first and compare with its prediction. Then
   report the result with `consult` before you edit. Edits are refused until you have.
5. If the answer gives an acceptance check, add it to your repro. Run it before and after the
   fix; it must pass when you are done.
6. Come back with what you observed when a prediction fails, when tests still fail after your
   fix, or when you are about to finish and unsure the fix matches the issue.

An existing test may expect the very behaviour the issue calls wrong. Then implement what the
issue asks anyway: do not edit that test and do not special-case the code to keep it passing.
Leave it failing and name it in your closing report; the maintainers update such tests with
the fix.

Advice is a second opinion, not the answer: check it against what you observe. To set a point
of advice aside you need an observation, a command's output that contradicts it; what you
remember about the library is not evidence.
