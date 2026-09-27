"""Parse test-runner output into per-test outcomes, for grading against fail/pass test lists."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

Outcome = Literal["passed", "failed", "skipped"]

# `  3/78 Test  #3: test-algorithms_cpp11 .........   Passed    0.05 sec`
# `  4/78 Test  #4: test-bjdata_cpp11 ..............***Failed    1.20 sec`
# `  5/78 Test  #5: test-bson ...................***Not Run   0.00 sec`
_CTEST = re.compile(r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(\S.*?)\s+\.+\s*(?:\*\*\*)?(\w+(?: Run)?)")
# Any other status (Failed, Timeout, Exception, Not Run, SEGFAULT, ...) means not passed.
_CTEST_SKIPPED = {"Skipped", "Disabled"}


def ctest(log: str, lower: bool = False) -> dict[str, Outcome]:
    """Outcomes from `ctest` progress lines; the last line for a test wins (reruns)."""
    results: dict[str, Outcome] = {}
    for line in log.splitlines():
        m = _CTEST.match(line)
        if not m:
            continue
        name, status = m.groups()
        if lower:
            name = name.lower()
        if status == "Passed":
            results[name] = "passed"
        elif status in _CTEST_SKIPPED:
            results[name] = "skipped"
        else:
            results[name] = "failed"
    return results


PARSERS: dict[str, Callable[[str], dict[str, Outcome]]] = {
    "ctest": ctest,
    # Multi-SWE-bench lower-cases Catch2's test names.
    "ctest-lower": lambda log: ctest(log, lower=True),
}
