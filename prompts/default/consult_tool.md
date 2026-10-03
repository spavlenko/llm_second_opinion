Ask a senior C++ engineer for a second opinion. They see only what you write here, plus the
latest error and code excerpts the harness attaches; they cannot run commands or open files.
Good moments: after your first failing test run when the cause is not obvious, before a change
to more than one file, or when the same error is back after a fix. You have {{max_consults}}
consults in total.

Arguments, each under {{field_target_words}} words:
- question: one specific question, e.g. "Why does `parse()` return early for an empty
  array?", not "How do I fix this?".
- tried: what you ran or read, and what you found.
- hypothesis: what you think causes the bug, and why.
