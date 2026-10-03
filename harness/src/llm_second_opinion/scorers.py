"""Scorers: rewards, dependence, and exposure, computed from stored attempt directories.

`bench score` re-runs them over the done items of an experiment without running any agent;
results go to `scores.json` in each attempt directory and `runs/<experiment>/scores.jsonl`.

Each score has a role (`ROLES`). The **acceptance** score of a help policy is binary resolve,
averaged over seeds; nothing else here may replace it. **Feedback** scores are dense signals
for the prompt-search proposer and for tie-breaking (gameable, so never the acceptance score).
**Diagnostic** scores guard the search and explain results: dependence on the advisor (the
executor must do the work itself) and exposure (what left the machine).

The formulas are in docs/spec.md, "Scorers"; the constants below are the ones it names.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_second_opinion.adapters.pi import _SHELL_EDIT, EDIT_TOOLS, TEST_COMMAND
from llm_second_opinion.config import ADVISOR_MODEL, Arm, ConfigError, Experiment, Price
from llm_second_opinion.contracts import ModelEndpoint, RoleUsage, UsageRecord
from llm_second_opinion.grading import diff_files
from llm_second_opinion.metering import (
    UPSTREAM_TIMEOUT_S,
    ItemMeter,
    MeteringError,
    MeteringProxy,
    cost_usd,
    preflight,
    read_usage,
)
from llm_second_opinion.tasks import Task

SCORER_VERSION = 1
P2P_PENALTY = 0.1  # `partial`: subtracted per broken pass-to-pass test
FUZZY_RATIO = 0.9  # `advice_copy_share`: a patch line this similar to an advice line is copied
SHINGLE = 40  # `brief_synthesis_share`: characters in a verbatim window
SIMILARITY_CHARS = 20_000  # `gold_similarity`: by characters up to this size, else by lines
PROBE_VERSION = 1  # bump when the probe prompt changes (part of the cache key)
DEFAULT_LAMBDA = 1.0  # `cost_penalised`: per USD of advisor cost
DEFAULT_MU = 0.01  # `exposure_penalised`: per 1000 advisor prompt tokens (or per leaked share)

ROLES: dict[str, str] = {
    "resolve": "acceptance",
    "partial": "feedback",
    "cost_penalised": "feedback",
    "exposure_penalised": "feedback",
    "gold_similarity": "diagnostic",
    "advice_copy_share": "diagnostic",
    "own_turns_before_consult": "diagnostic",
    "own_tool_calls_before_consult": "diagnostic",
    "own_reads_before_consult": "diagnostic",
    "own_edits_before_consult": "diagnostic",
    "own_test_runs_before_consult": "diagnostic",
    "consults": "diagnostic",
    "consult_rate": "diagnostic",
    "consult_refusals": "diagnostic",
    "consults_on_a0_solved": "diagnostic",
    "brief_synthesis_share": "diagnostic",
    "advisor_prompt_tokens": "diagnostic",
    "advisor_completion_tokens": "diagnostic",
    "advisor_cost_usd": "diagnostic",
    "placeholders_sent": "diagnostic",
    "leaked_units": "diagnostic",
    "role_map_leaks": "diagnostic",
    "probe_*": "diagnostic",
}

# C++ keywords and well-known standard names, as the plugin's redaction keeps them
# (plugin/advisor-core/src/redact.ts): not project identifiers.
_KEYWORD_TEXT = (
    "alignas alignof and asm auto bool break case catch char char8_t char16_t char32_t "
    "class const consteval constexpr constinit const_cast continue co_await co_return "
    "co_yield decltype default delete do double dynamic_cast else enum explicit export "
    "extern false float for friend goto if inline int long mutable namespace new "
    "noexcept not nullptr operator or private protected public register reinterpret_cast "
    "requires return short signed sizeof static static_assert static_cast struct switch "
    "template this thread_local throw true try typedef typeid typename union unsigned "
    "using virtual void volatile wchar_t while override final include define ifdef "
    "ifndef endif elif pragma undef defined NULL main std size_t ssize_t ptrdiff_t "
    "nullptr_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t "
    "uintptr_t intptr_t string string_view vector map set unordered_map unordered_set "
    "array pair tuple optional variant unique_ptr shared_ptr weak_ptr make_unique "
    "make_shared move forward swap begin end size empty push_back emplace_back cout cerr "
    "endl printf assert abs min max"
)
KEYWORDS = frozenset(_KEYWORD_TEXT.split())
GENERIC_PROJECT_WORDS = frozenset(["json", "xml", "yaml", "toml", "csv", "http", "sql", "regex"])
PLACEHOLDER = re.compile(
    r"<(?:function|type|macro|namespace|variable|file|test|identifier|project)_\d+>"
)
# Lines no one copies: a patch line reduced to letters and digits that is one of these.
_TRIVIAL_WORDS = frozenset(
    ["else", "endif", "break", "continue", "return", "default", "public", "private"]
    + ["protected", "try", "do"]
)


# --- reading an attempt ----------------------------------------------------------------


def jsonl(path: Path) -> list[dict]:
    """Records of a JSONL file; unreadable lines are skipped, a missing file is empty."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class Attempt:
    """One stored attempt (or an old-layout `seed-<n>/` directory) and its task."""

    dir: Path
    rel: str  # relative to runs/<experiment>/
    arm: Arm | None
    task: Task
    seed: int
    config_hash: str
    events: list[dict] = field(default_factory=list)
    grade: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    advisor: dict = field(default_factory=dict)
    patch: str = ""
    usage: list[UsageRecord] = field(default_factory=list)
    resolved: bool = False

    @classmethod
    def load(
        cls, exp_dir: Path, rel: str, arm: Arm | None, task: Task, row: Mapping[str, Any]
    ) -> Attempt:
        d = exp_dir / rel
        patch = d / "patch.diff"
        grade = _json(d / "grade.json")
        resolved = grade.get("resolved", row.get("resolved"))
        return cls(
            dir=d,
            rel=rel,
            arm=arm,
            task=task,
            seed=int(row["seed"]),
            config_hash=str(row["config_hash"]),
            events=jsonl(d / "events.jsonl"),
            grade=grade,
            result=_json(d / "result.json") or _json(d / "agent-result.json"),
            metrics=_json(d / "metrics.json"),
            advisor=_json(d / "advisor.json"),
            patch=patch.read_text(errors="replace") if patch.exists() else "",
            usage=read_usage(d / "usage.jsonl"),
            resolved=bool(resolved),
        )

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def briefs(self) -> list[str]:
        return [str(e.get("brief_text") or "") for e in self.of("advisor_request")]

    @property
    def level(self) -> str | None:
        built = self.of("brief_built")
        if built:
            return built[-1].get("level")
        return (self.advisor.get("advisor") or {}).get("level")

    @property
    def cloud_executor(self) -> bool:
        """The executor is the advisor model (A4): everything it reads goes to the cloud."""
        return self.arm is not None and self.arm.executor == ADVISOR_MODEL


def attempt_rel(row: Mapping[str, Any]) -> str:
    """The counted attempt's directory, or the item's own `seed-<n>/` for the layout before
    attempt directories."""
    return row.get("attempt_dir") or f"{row['arm']}/{row['task']}/seed-{row['seed']}"


# --- diffs -------------------------------------------------------------------------------


_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$")


def changed_lines(diff: str) -> dict[str, list[str]]:
    """Per file (new path): the added and removed lines with their sign, in order."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_hunk = False
    for line in diff.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            current = files.setdefault(header[2], [])
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if current is None or not in_hunk:
            continue
        if line[:1] in "+-" and not line.startswith(("+++ ", "--- ")):
            current.append(line[0] + line[1:].rstrip())
    return files


def hunk_contexts(diff: str) -> list[str]:
    """The text after each hunk header (git puts the enclosing function there)."""
    return [m[1] for line in diff.splitlines() if (m := _HUNK.match(line)) and m[1]]


def added_lines(diff: str) -> list[str]:
    return [line[1:] for lines in changed_lines(diff).values() for line in lines if line[0] == "+"]


def normalise(line: str) -> str:
    return " ".join(line.split())


def trivial(line: str) -> bool:
    """A line nobody copies: fewer than 3 letters or digits (`}`, `});`, `#if 0`), or a lone
    keyword (`} else {`, `break;`)."""
    letters = re.sub(r"[^0-9A-Za-z_]", "", line)
    return len(letters) < 3 or letters in _TRIVIAL_WORDS


# --- rewards -------------------------------------------------------------------------------


def partial_score(grade: Mapping[str, Any], resolved: bool) -> float:
    """F2P share passing minus P2P_PENALTY per broken P2P test, clipped at 0; 0 when the
    build failed or nothing was tested (empty, unappliable patch, timeout). A task without
    test lists (toy) scores its resolve."""
    if resolved:
        return 1.0
    if grade.get("build_failed"):
        return 0.0
    f2p, p2p = grade.get("f2p"), grade.get("p2p")
    if not f2p or not f2p.get("total"):
        return 0.0
    broken = (p2p["total"] - p2p["passed"]) if p2p else 0
    return max(0.0, f2p["passed"] / f2p["total"] - P2P_PENALTY * broken)


def gold_similarity(patch: str, gold: str) -> float | None:
    """SWE-RL-style similarity: difflib's ratio between the changed lines (with their +/-
    sign, whitespace-normalised, files in path order, joined by newlines) of the agent's
    patch and of the gold patch, by characters; 0 for an empty patch, None without a gold
    patch. Above SIMILARITY_CHARS characters on either side, by lines instead (difflib is
    quadratic; such patches run to thousands of lines)."""
    if not gold.strip():
        return None
    if not patch.strip():
        return 0.0

    def seq(diff: str) -> list[str]:
        files = changed_lines(diff)
        return [normalise(line) for name in sorted(files) for line in files[name]]

    a, b = seq(gold), seq(patch)
    text_a, text_b = "\n".join(a), "\n".join(b)
    if max(len(text_a), len(text_b)) > SIMILARITY_CHARS:
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return difflib.SequenceMatcher(None, text_a, text_b, autojunk=False).ratio()


def advisor_spend(
    usage: Iterable[UsageRecord], cloud_executor: bool, price: Price | None
) -> dict[str, Any]:
    """Calls to the advisor model (and, for an arm whose executor is that model, the
    executor's calls too): prompt and completion tokens as the provider counted them, calls,
    and cost (None when the model has no price and made calls)."""
    roles = {"advisor", "executor"} if cloud_executor else {"advisor"}
    calls = [r for r in usage if r.role in roles and 200 <= r.status < 300]
    total = RoleUsage(
        role="advisor",
        calls=len(calls),
        prompt_tokens=sum(r.prompt_tokens for r in calls),
        completion_tokens=sum(r.completion_tokens for r in calls),
        cached_tokens=sum(r.cached_tokens for r in calls),
        reasoning_tokens=sum(r.reasoning_tokens for r in calls),
    )
    cost = 0.0 if not calls else cost_usd([total], {"advisor": price})
    return {
        "advisor_calls": total.calls,
        "advisor_prompt_tokens": total.prompt_tokens,
        "advisor_completion_tokens": total.completion_tokens,
        "advisor_cost_usd": cost,
    }


# --- dependence ----------------------------------------------------------------------------


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def advice_lines(texts: Iterable[str]) -> set[str]:
    """Candidate lines from advice: every line (a leading diff `+`/`-` dropped), and every
    inline code span, whitespace-normalised."""
    out = set()
    for text in texts:
        for line in text.splitlines():
            out.add(normalise(re.sub(r"^\s*[+-](?=\s|\S)", "", line, count=1)))
            out.add(normalise(line))
            out.update(normalise(m) for m in _INLINE_CODE.findall(line))
    return {line for line in out if line and not trivial(line)}


def copied(line: str, candidates: set[str]) -> bool:
    if line in candidates:
        return True
    for c in candidates:
        # ratio <= 2 * min(len) / (len + len): skip candidates that cannot reach the bar.
        if 2 * min(len(c), len(line)) / (len(c) + len(line)) < FUZZY_RATIO:
            continue
        m = difflib.SequenceMatcher(None, line, c, autojunk=False)
        if m.quick_ratio() >= FUZZY_RATIO and m.ratio() >= FUZZY_RATIO:
            return True
    return False


def advice_copy_share(patch: str, advice: Iterable[str]) -> tuple[float | None, int, int]:
    """(share, copied, considered): the share of the patch's non-trivial added lines that
    appear in the advice, exactly or with difflib ratio >= FUZZY_RATIO (whitespace
    normalised). None when the patch adds no such line."""
    lines = [n for n in (normalise(line) for line in added_lines(patch)) if not trivial(n)]
    if not lines:
        return None, 0, 0
    candidates = advice_lines(advice)
    hits = sum(copied(line, candidates) for line in lines) if candidates else 0
    return hits / len(lines), hits, len(lines)


def consult_starts(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") in ("consult_requested", "trigger_fired")]


def tool_calls(pi_events: list[dict], timeline: list[dict]) -> list[dict]:
    """pi's tool calls in order: name, kind (`read`, `edit`, `test`, or `consult`), and
    start time in ms from the timeline extension (None without one)."""
    starts = {m.get("id"): m.get("t") for m in timeline if m.get("event") == "tool_start"}
    calls = []
    for e in pi_events:
        if e.get("type") != "tool_execution_start":
            continue
        name = e.get("toolName") or "?"
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        command = str(args.get("command") or "") if name == "bash" else ""
        if name == "consult":
            kind = "consult"
        elif name in EDIT_TOOLS or _SHELL_EDIT.search(command):
            kind = "edit"
        elif TEST_COMMAND in command:
            kind = "test"
        else:
            kind = "read"  # the read tool, and any other command: looking around
        calls.append({"name": name, "kind": kind, "t": starts.get(e.get("toolCallId"))})
    return calls


def own_work(
    events: list[dict], pi_events: list[dict], timeline: list[dict]
) -> dict[str, int | None]:
    """The executor's own work before its first consult (whoever started it): turns before
    it, and tool calls (reads, edits, test runs) that started before the consult's first
    event, by the timeline's clock. All None when there was no consult; the call counts None
    without a timeline."""
    keys = ("turns", "tool_calls", "reads", "edits", "test_runs")
    names = {k: f"own_{k}_before_consult" for k in keys}
    starts = consult_starts(events)
    if not starts:
        return dict.fromkeys(names.values())
    first = starts[0]
    out: dict[str, int | None] = {names["turns"]: first.get("turn")}
    calls = tool_calls(pi_events, timeline)
    if not timeline or "ts" not in first:
        return out | dict.fromkeys(list(names.values())[1:])
    before = [c for c in calls if c["kind"] != "consult" and c["t"] is not None
              and c["t"] <= first["ts"] * 1000]  # fmt: skip
    out[names["tool_calls"]] = len(before)
    out[names["reads"]] = sum(c["kind"] == "read" for c in before)
    out[names["edits"]] = sum(c["kind"] == "edit" for c in before)
    out[names["test_runs"]] = sum(c["kind"] == "test" for c in before)
    return out


def tool_outputs(pi_events: list[dict]) -> list[tuple[float | None, str]]:
    """(time in ms, text) of every tool result the executor saw, the advisor's own answers
    (the consult tool's results) left out."""
    out = []
    for e in pi_events:
        message = e.get("message") if e.get("type") == "message_end" else None
        if not isinstance(message, dict) or message.get("role") != "toolResult":
            continue
        if message.get("toolName") == "consult":
            continue
        content = message.get("content")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
        )
        out.append((message.get("timestamp"), text))
    return out


def unredact(text: str, role_map: Mapping[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: role_map.get(m[0], m[0]), text)


def template_lines(template: str) -> set[str]:
    """The literal lines of the `brief` template (those without a placeholder)."""
    return {normalise(line) for line in template.splitlines() if line.strip() and "{{" not in line}


def synthesis(brief: str, sources: list[str], template: set[str]) -> tuple[int, int]:
    """(considered, copied) characters of one brief: template lines are left out; a
    character is copied when it lies in a SHINGLE-character window (whitespace collapsed)
    found verbatim in a source."""
    text = normalise(
        " ".join(line for line in brief.splitlines() if normalise(line) not in template)
    )
    if len(text) < SHINGLE:
        return len(text), 0
    starts: dict[str, list[int]] = {}
    for i in range(len(text) - SHINGLE + 1):
        starts.setdefault(text[i : i + SHINGLE], []).append(i)
    covered = bytearray(len(text))
    found: set[str] = set()
    for source in sources:
        s = normalise(source)
        for i in range(len(s) - SHINGLE + 1):
            w = s[i : i + SHINGLE]
            if w in starts and w not in found:
                found.add(w)
                for j in starts[w]:
                    covered[j : j + SHINGLE] = b"\x01" * SHINGLE
    return len(text), sum(covered)


def brief_synthesis(
    events: list[dict], pi_events: list[dict], issue: str, template: str
) -> tuple[float | None, list[float]]:
    """(run share, per-brief shares): the share of each brief's characters (template lines
    left out, placeholders mapped back to names with the run's role map) not inside a
    SHINGLE-character window copied verbatim from a tool result the executor had seen by
    then, or from the issue. None without briefs."""
    outputs = tool_outputs(pi_events)
    literal = template_lines(template)
    role_map: dict[str, str] = {}
    considered = copied_chars = 0
    shares = []
    for e in events:
        if e.get("type") == "brief_built":
            role_map |= e.get("role_map") or {}
        if e.get("type") != "advisor_request":
            continue
        seen_by = float(e.get("ts") or 0) * 1000
        sources = [text for t, text in outputs if t is None or t <= seen_by] + [issue]
        n, c = synthesis(unredact(str(e.get("brief_text") or ""), role_map), sources, literal)
        considered += n
        copied_chars += c
        shares.append(1 - c / n if n else 1.0)
    if not shares:
        return None, []
    return (1 - copied_chars / considered if considered else 1.0), shares


def consult_counts(events: list[dict], turns: int | None) -> dict[str, Any]:
    consults = len(consult_starts(events))
    return {
        "consults": consults,
        "consult_rate": consults / turns if turns else None,
        "consult_refusals": sum(e.get("type") == "consult_refused" for e in events),
    }


# --- exposure -------------------------------------------------------------------------------


def leaks(name: str, text: str) -> bool:
    """A name appearing raw as a whole name, by the plugin's own sweep rule (and
    scripts/smoke-check.py): plain lowercase words under 8 letters are not counted."""
    if len(name) < 3 or re.fullmatch(r"[a-z]{1,7}", name):
        return False
    return re.search(rf"(?<![\w/.]){re.escape(name)}(?![\w/])", text) is not None


def file_leaks(path: str, text: str) -> bool:
    """A file path, or its file name, appearing raw."""
    if path in text:
        return True
    name = path.rsplit("/", 1)[-1]
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text) is not None


_COMMENT = re.compile(r"//.*$|/\*.*?\*/|/\*.*$|^\s*\*.*$")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_NAME = re.compile(r"[A-Za-z_]\w*")


def code_names(line: str) -> list[str]:
    """Project names in a code line: comments, string literals and `std::` names removed;
    C++ keywords, names under 3 characters, and plain lowercase words under 8 letters (the
    plugin leaves those raw by design) left out."""
    line = _COMMENT.sub("", _STRING.sub('""', line))
    line = re.sub(r"\bstd::\w+", "", line)
    return [
        n
        for n in _NAME.findall(line)
        if n not in KEYWORDS and len(n) >= 3 and not re.fullmatch(r"[a-z]{1,7}", n)
    ]


def gold_units(gold: str) -> tuple[list[str], list[str]]:
    """(identifiers, files) of the gold patch: the names in its changed lines and hunk
    headers, and the paths it changes."""
    names: set[str] = set()
    for lines in changed_lines(gold).values():
        for line in lines:
            names.update(code_names(line[1:]))
    for context in hunk_contexts(gold):
        names.update(code_names(context))
    return sorted(names), sorted(diff_files(gold))


def leaked_units(briefs: list[str], gold: str) -> tuple[float | None, list[str], int]:
    """(share, leaked, units): PAPILLON-style, the share of the gold patch's identifiers and
    file paths that appear raw in any brief. None without a gold patch."""
    identifiers, files = gold_units(gold)
    units = len(identifiers) + len(files)
    if not units:
        return None, [], 0
    text = "\n".join(briefs)
    leaked = [n for n in identifiers if leaks(n, text)] + [f for f in files if file_leaks(f, text)]
    return len(leaked) / units, sorted(leaked), units


def role_map_leaks(events: list[dict]) -> list[str]:
    """Names of the run's role map that appear raw in a brief (by `leaks`): what the
    redaction meant to hide and sent anyway. Counted against the role map known when each
    brief was sent."""
    names: dict[str, str] = {}
    found: set[str] = set()
    for e in events:
        if e.get("type") == "brief_built":
            names |= e.get("role_map") or {}
        elif e.get("type") == "advisor_request":
            brief = str(e.get("brief_text") or "")
            found.update(n for n in names.values() if leaks(n, brief))
    return sorted(found)


def placeholders(briefs: list[str]) -> tuple[int, int]:
    """(occurrences, distinct) of role placeholders in the briefs sent."""
    found = [m for b in briefs for m in PLACEHOLDER.findall(b)]
    return len(found), len(set(found))


# --- re-identification probe ----------------------------------------------------------------


PROBE_SYSTEM = "You identify open-source code from descriptions of it."
PROBE_PROMPT = """Below is text that a developer sent to an outside advisor about a bug in a \
public open-source C++ project. Some names may be replaced with placeholders such as \
<function_1> or <file_2>.

Guess which repository it comes from, which source files the fix changes, and which \
functions. Answer with JSON only, up to 3 guesses each, most likely first:
{"repository": ["org/name", ...], "files": ["path/to/file", ...], "functions": ["name", ...]}

<text>
{text}
</text>"""


@dataclass
class Truth:
    repo: str | None  # org/name
    files: list[str]
    functions: list[str]


def _calls(text: str) -> list[str]:
    text = _COMMENT.sub("", _STRING.sub('""', text))
    return [n for n in re.findall(r"([A-Za-z_]\w*)\s*\(", text) if n not in KEYWORDS]


def enclosing_definitions(diff: str) -> list[str]:
    """Per hunk, the function defined by the last context line before its first change that
    opens a body (`name(...) ... {`): the function the change is in, when git's hunk header
    names a class instead."""
    found: list[str] = []
    last: str | None = None
    changed = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            last, changed = None, False
        elif line.startswith(" ") and not changed:
            names = _calls(line)
            if names and line.rstrip().endswith("{"):
                last = names[0]
        elif line[:1] in "+-" and not line.startswith(("+++ ", "--- ")) and not changed:
            changed = True
            if last:
                found.append(last)
    return found


def truth_of(task: Task) -> Truth:
    """The repository (from the task, or its id: `org__name-123`), and the gold patch's files
    and functions: the names before `(` in its hunk headers and changed lines, and the
    function each hunk's change sits in (`enclosing_definitions`)."""
    repo = task.repo
    if repo is None and (m := re.fullmatch(r"(.+?)__(.+)-\d+", task.id)):
        repo = f"{m[1]}/{m[2]}"
    texts = hunk_contexts(task.gold_patch) + [
        line[1:] for lines in changed_lines(task.gold_patch).values() for line in lines
    ]
    functions = {n for text in texts for n in _calls(text)}
    functions.update(enclosing_definitions(task.gold_patch))
    return Truth(repo, sorted(diff_files(task.gold_patch)), sorted(functions))


def parse_guesses(answer: str) -> dict[str, list[str]]:
    """The probe's JSON answer (the first `{` to the last `}`), as lists of strings."""
    out: dict[str, list[str]] = {"repository": [], "files": [], "functions": []}
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return out
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    for key in out:
        value = data.get(key)
        values = value if isinstance(value, list) else [value] if value else []
        out[key] = [str(v).strip() for v in values if str(v).strip()]
    return out


def _repo_hit(guess: str, repo: str) -> bool:
    g = guess.lower().strip().strip("/").removesuffix(".git")
    org, name = repo.lower().split("/", 1)
    tail = "/".join(g.split("/")[-2:])
    return tail == f"{org}/{name}" or (g == name and name not in GENERIC_PROJECT_WORDS)


def _file_hit(guess: str, files: list[str]) -> bool:
    name = guess.strip().rsplit("/", 1)[-1]
    return any(f.rsplit("/", 1)[-1] == name for f in files)


def _function_hit(guess: str, functions: list[str]) -> bool:
    name = re.sub(r"\(.*$", "", guess.strip()).rsplit("::", 1)[-1].strip()
    return name in functions


def probe_scores(guesses: dict[str, list[str]], truth: Truth, prefix: str) -> dict[str, Any]:
    """Top-1 and top-3 hits for repository, file and function (None when the truth is
    unknown, e.g. a toy task has no repository)."""
    checks: dict[str, tuple[list[str], Callable[[str], bool]] | None] = {
        "repo": (
            (guesses["repository"], lambda g: _repo_hit(g, truth.repo)) if truth.repo else None
        ),
        "file": (guesses["files"], lambda g: _file_hit(g, truth.files)) if truth.files else None,
        "function": (
            (guesses["functions"], lambda g: _function_hit(g, truth.functions))
            if truth.functions
            else None
        ),
    }
    out: dict[str, Any] = {}
    for name, check in checks.items():
        if check is None:
            out[f"{prefix}_{name}_top1"] = out[f"{prefix}_{name}_top3"] = None
            continue
        values, hit = check
        out[f"{prefix}_{name}_top1"] = float(bool(values) and hit(values[0]))
        out[f"{prefix}_{name}_top3"] = float(any(hit(g) for g in values[:3]))
    return out


def redact_issue(issue: str, level: str | None, role_map: Mapping[str, str]) -> str:
    """The issue text redacted at a level, for the memorisation floor: an approximation of
    the plugin's redaction that uses the run's own role map. L3 is verbatim; below it every
    role-map name is replaced by its placeholder (whole names, longest first); at L0 and L1
    code blocks become `[code omitted]` as well."""
    if level == "L3" or level is None:
        return issue
    text = issue
    if level in ("L0", "L1"):
        text = re.sub(r"```.*?```", "[code omitted]", text, flags=re.DOTALL)
    for placeholder, name in sorted(role_map.items(), key=lambda kv: -len(kv[1])):
        if len(name) < 2:
            continue
        text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", placeholder, text)
    return text


def full_role_map(events: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in events:
        if e.get("type") == "brief_built":
            out |= e.get("role_map") or {}
    return out


class Prober:
    """Asks a model (a key in the experiment's `models`) to re-identify the source of a text,
    through the metering proxy with the usage role `probe`. Answers are cached by the hash
    of the text and model, so scoring again does not query again. Each attempt's calls go to
    its `probe-usage.jsonl`, and their spend to the ledger's `probe` table."""

    def __init__(
        self,
        exp: Experiment,
        key: str,
        cache_dir: Path,
        env: Mapping[str, str],
        *,
        record: Callable[..., None] | None = None,
        host: str = "127.0.0.1",
    ):
        if key not in exp.models:
            raise ConfigError(f"--probe {key!r} is not in models: {', '.join(exp.models)}")
        self.key = key
        self.endpoint: ModelEndpoint = exp.models[key]
        self.price = exp.prices.get(key)
        self.cache_dir = cache_dir
        self.env = env
        self.record = record
        self.host = host
        self.proxy: MeteringProxy | None = None
        self.calls = 0

    def start(self, preflight_dir: Path | None) -> Prober:
        self.proxy = MeteringProxy(self.host, env=self.env).start()
        if preflight_dir is not None:
            preflight(self.proxy, {self.key: self.endpoint}, preflight_dir)
        return self

    def stop(self) -> None:
        if self.proxy:
            self.proxy.stop()
            self.proxy = None

    def cache_key(self, text: str) -> str:
        payload = json.dumps([PROBE_VERSION, self.endpoint.model, text])
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    @contextmanager
    def attempt(self, out_dir: Path) -> Iterator[Callable[[str], dict[str, list[str]]]]:
        """`ask(text)` for one attempt; its calls are metered into `probe-usage.jsonl`."""
        meter: ItemMeter | None = None
        usage = out_dir / "probe-usage.jsonl"
        before = len(read_usage(usage))

        def ask(text: str) -> dict[str, list[str]]:
            nonlocal meter
            path = self.cache_dir / f"{self.cache_key(text)}.json"
            if path.exists():
                return parse_guesses(_json(path).get("answer", ""))
            if self.proxy is None:
                raise MeteringError("the probe's proxy is not running")
            if meter is None:
                meter = self.proxy.register({"probe": self.endpoint}, usage, None)
            answer = self._call(self.proxy.url(meter, "probe", "127.0.0.1"), text)
            self.calls += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"model": self.endpoint.model, "answer": answer}))
            return parse_guesses(answer)

        try:
            yield ask
        finally:
            if meter is not None and self.proxy is not None:
                try:
                    meter.close(UPSTREAM_TIMEOUT_S)
                finally:
                    self.proxy.unregister(meter)
                    new = read_usage(usage)[before:]
                    if self.record and new:
                        self.record(out_dir, new, self._cost(new))

    def _cost(self, records: list[UsageRecord]) -> float | None:
        ok = [r for r in records if 200 <= r.status < 300]
        total = RoleUsage(
            role="probe",
            calls=len(ok),
            prompt_tokens=sum(r.prompt_tokens for r in ok),
            completion_tokens=sum(r.completion_tokens for r in ok),
            cached_tokens=sum(r.cached_tokens for r in ok),
            reasoning_tokens=sum(r.reasoning_tokens for r in ok),
        )
        return cost_usd([total], {"probe": self.price}) if ok else 0.0

    def _call(self, url: str, text: str) -> str:
        e = self.endpoint
        body: dict[str, Any] = {
            "model": e.model,
            "messages": [
                {"role": "system", "content": PROBE_SYSTEM},
                {"role": "user", "content": PROBE_PROMPT.replace("{text}", text)},
            ],
        }
        sampling = {"temperature": e.temperature, "top_p": e.top_p, "seed": e.sampling_seed,
                    "reasoning_effort": e.reasoning_effort}  # fmt: skip
        body |= {k: v for k, v in sampling.items() if v is not None}
        request = urllib.request.Request(
            f"{url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=UPSTREAM_TIMEOUT_S) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise MeteringError(f"probe call failed: HTTP {err.code}: {detail}") from err
        except (OSError, ValueError) as err:
            raise MeteringError(f"probe call failed: {type(err).__name__}: {err}") from err
        try:
            return str(reply["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""


# --- one attempt ---------------------------------------------------------------------------


@dataclass
class Params:
    """Weights of the penalised rewards; recorded in every scores.json."""

    cost_lambda: float = DEFAULT_LAMBDA  # per USD of advisor cost
    exposure_mu: float = DEFAULT_MU
    exposure_unit: str = "ktok"  # `ktok`: advisor prompt tokens / 1000; `leaked`: leaked_units


def score_attempt(
    att: Attempt,
    params: Params,
    price: Price | None = None,
    a0_resolved: bool | None = None,
    ask: Callable[[str], dict[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """Every score of one attempt, as the record written to scores.json."""
    resolve = float(att.resolved)
    pi_events = jsonl(att.dir / "pi.jsonl")
    timeline = jsonl(att.dir / "timeline.jsonl")
    turns = att.result.get("turns", att.metrics.get("turns"))
    if turns is None and pi_events:
        turns = sum(e.get("type") == "turn_end" for e in pi_events)
    briefs = att.briefs
    advice = [str(e.get("advice_text") or "") for e in att.of("advisor_response")]
    advice += [str(e.get("injected_text") or "") for e in att.of("advice_applied")]

    scores: dict[str, Any] = {"resolve": resolve, "partial": partial_score(att.grade, att.resolved)}
    scores["gold_similarity"] = gold_similarity(att.patch, att.task.gold_patch)
    spent = advisor_spend(att.usage, att.cloud_executor, price)
    cost = spent["advisor_cost_usd"]
    scores["cost_penalised"] = None if cost is None else resolve - params.cost_lambda * cost

    share, n_copied, n_added = advice_copy_share(att.patch, advice)
    scores |= {"advice_copy_share": share, "advice_copied_lines": n_copied,
               "patch_added_lines": n_added}  # fmt: skip
    scores |= own_work(att.events, pi_events, timeline)
    scores |= consult_counts(att.events, turns)
    starts = len(consult_starts(att.events))
    scores["a0_resolved"] = None if a0_resolved is None else float(a0_resolved)
    scores["consults_on_a0_solved"] = (
        None if a0_resolved is None else (starts if a0_resolved else 0)
    )
    template = ((att.advisor.get("prompts") or {}).get("texts") or {}).get("brief", "")
    run_share, per_brief = brief_synthesis(
        att.events, pi_events, att.task.problem_statement, template
    )
    scores["brief_synthesis_share"] = run_share
    scores["brief_synthesis_by_brief"] = per_brief

    scores |= spent
    sent, distinct = placeholders(briefs)
    scores |= {"briefs": len(briefs), "placeholders_sent": sent, "placeholders_distinct": distinct}
    if att.cloud_executor:  # the advisor model is the executor: it reads the code itself
        identifiers, files = gold_units(att.task.gold_patch)
        units = identifiers + files
        leaked, names, n_units = (1.0 if units else None), units, len(units)
    else:
        leaked, names, n_units = leaked_units(briefs, att.task.gold_patch)
    scores |= {"leaked_units": leaked, "leaked_units_list": names, "gold_units": n_units}
    role_leaks = role_map_leaks(att.events)
    scores |= {"role_map_leaks": len(role_leaks), "role_map_leaks_list": role_leaks}
    if params.exposure_unit == "leaked":
        exposure = leaked
    else:
        exposure = spent["advisor_prompt_tokens"] / 1000
    scores["exposure_penalised"] = (
        None if exposure is None else resolve - params.exposure_mu * exposure
    )

    if ask is not None and briefs:
        truth = truth_of(att.task)
        separator = "\n\n----- next message -----\n\n"
        scores |= probe_scores(ask(separator.join(briefs)), truth, "probe")
        floor = redact_issue(att.task.problem_statement, att.level, full_role_map(att.events))
        scores |= probe_scores(ask(floor), truth, "probe_floor")
    return scores


# --- an experiment -------------------------------------------------------------------------


def score_record(
    exp: Experiment, att: Attempt, scores: dict[str, Any], params: Params, probe: str | None
) -> dict[str, Any]:
    return {
        "scorer_version": SCORER_VERSION,
        "scored": time.time(),
        "experiment": exp.name,
        "arm": att.arm.name if att.arm else None,
        "task": att.task.id,
        "seed": att.seed,
        "config_hash": att.config_hash,
        "attempt_dir": att.rel,
        "level": att.level,
        "params": {"cost_lambda": params.cost_lambda, "exposure_mu": params.exposure_mu,
                   "exposure_unit": params.exposure_unit, "p2p_penalty": P2P_PENALTY,
                   "fuzzy_ratio": FUZZY_RATIO, "shingle": SHINGLE,
                   "probe_model": probe, "probe_version": PROBE_VERSION if probe else None},
        "roles": ROLES,
        "scores": scores,
    }  # fmt: skip


def read_scores(exp_dir: Path) -> dict[str, dict[str, Any]]:
    """runs/<experiment>/scores.jsonl, by attempt directory."""
    return {r["attempt_dir"]: r for r in jsonl(exp_dir / "scores.jsonl") if "attempt_dir" in r}


def write_scores(exp_dir: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Merge records into scores.jsonl (a re-scored attempt replaces its line)."""
    merged = read_scores(exp_dir) | {r["attempt_dir"]: r for r in records}
    path = exp_dir / "scores.jsonl"
    lines = [json.dumps(merged[k]) for k in sorted(merged)]
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def a0_outcomes(rows: list[dict[str, Any]], baseline: str = "A0") -> dict[tuple, bool]:
    """Resolved or not per (task, seed) of the baseline arm (rows already current)."""
    return {
        (r["task"], r["seed"]): bool(r["resolved"])
        for r in rows
        if r["arm"] == baseline and r["status"] == "done"
    }


def numeric_scores(scores: Mapping[str, Any]) -> dict[str, float]:
    """The scores MLflow can log, as `score_<name>`."""
    return {
        f"score_{k}": float(v)
        for k, v in scores.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


DIAGNOSTIC_KEYS = (
    "partial",
    "gold_similarity",
    "advice_copy_share",
    "consult_rate",
    "brief_synthesis_share",
    "leaked_units",
    "probe_repo_top1",
    "probe_floor_repo_top1",
)


def arm_means(records: Iterable[Mapping[str, Any]], keys: Iterable[str] = DIAGNOSTIC_KEYS):
    """Per arm: the number of scored attempts and the mean of each key over the attempts
    where it is defined (None where it never is)."""
    by_arm: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        by_arm.setdefault(r["arm"], []).append(r["scores"])
    out = {}
    for arm, scores in by_arm.items():
        means: dict[str, float | None] = {"scored": len(scores)}
        for k in keys:
            values = [s[k] for s in scores if isinstance(s.get(k), (int, float))]
            means[k] = sum(values) / len(values) if values else None
        out[arm] = means
    return out


def format_diagnostics(means: Mapping[str, Mapping[str, Any]], arms: list[str]) -> str:
    """The per-arm block of `bench report`. Not the acceptance score (that is resolve)."""
    title = "Diagnostics (not acceptance): means over scored attempts, from `bench score`"
    if not means:
        return f"{title}\n  none: run `bench score` first"
    keys = [k for k in DIAGNOSTIC_KEYS if any(m.get(k) is not None for m in means.values())]
    short = {"partial": "partial", "gold_similarity": "gold sim", "advice_copy_share": "copy",
             "consult_rate": "consult/turn", "brief_synthesis_share": "synth",
             "leaked_units": "leaked", "probe_repo_top1": "probe repo@1",
             "probe_floor_repo_top1": "floor repo@1"}  # fmt: skip
    header = f"{'arm':<12} {'scored':>6} " + " ".join(f"{short[k]:>12}" for k in keys)
    lines = [title, header, "-" * len(header)]
    for arm in arms:
        m = means.get(arm)
        if not m:
            continue
        cells = " ".join(f"{_fmt(m.get(k)):>12}" for k in keys)
        lines.append(f"{arm:<12} {m['scored']:>6} {cells}")
    lines.append(
        "partial = F2P share - 0.1 per broken P2P (feedback); copy = patch lines found in the "
        "advice; synth = brief share not copied from tool output; leaked = gold-patch names "
        "and files sent raw; probe repo@1 = repository named first from the briefs (floor: "
        "from the issue alone). Resolve stays the only acceptance score."
    )
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def probe_env(endpoint: ModelEndpoint, environ: Mapping[str, str]) -> dict[str, str]:
    """The secrets the probe endpoint names, from the host environment (for the proxy)."""
    names = {endpoint.api_key_env, *endpoint.header_env.values()} - {None}
    missing = sorted(n for n in names if n not in environ)
    if missing:
        raise ConfigError(f"unset secret variables for the probe: {', '.join(missing)}")
    return {n: environ[n] for n in names}


def score_experiment(
    exp: Experiment,
    runs_dir: Path,
    *,
    arm: str | None = None,
    task: str | None = None,
    params: Params | None = None,
    prober: Prober | None = None,
    tracker: Any = None,
    echo: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Score the counted attempt of every done item at its arm's current config hash (as
    `bench report` counts them), write scores.json in each attempt directory and merge the
    records into runs/<experiment>/scores.jsonl. With a tracker, item runs get `score_*`
    metrics and arm runs `score_mean_*`."""
    from llm_second_opinion.ledger import Ledger
    from llm_second_opinion.report import current_rows, recorded_hashes
    from llm_second_opinion.runner import task_image
    from llm_second_opinion.tasks import Manifest

    params = params or Params()
    exp_dir = runs_dir / exp.name
    ledger = Ledger(exp_dir / "ledger.sqlite")
    tasks = {t.id: t for t in exp.select(Manifest.from_yaml(exp.tasks)).tasks}
    if task is not None and task not in tasks:
        raise ConfigError(f"no task {task!r} in {exp.tasks}")
    arms = {a.name: a for a in exp.arms}
    if arm is not None and arm not in arms:
        raise ConfigError(f"no arm named {arm!r}; arms: {', '.join(arms)}")
    hashes = recorded_hashes(exp, ledger.fingerprints(exp.name))
    images = {t.id: task_image(t) for t in tasks.values()}
    rows = [
        r
        for r in current_rows(exp, ledger.rows(exp.name), hashes, images)
        if r["status"] == "done" and r["task"] in tasks
    ]
    a0 = a0_outcomes(rows) if "A0" in arms else None
    price = exp.prices.get(ADVISOR_MODEL)
    todo = [r for r in rows if arm in (None, r["arm"]) and task in (None, r["task"])]
    echo(f"scoring {len(todo)} done item(s)" + (f", probe {prober.key}" if prober else ""))
    records = []
    for row in todo:
        rel = attempt_rel(row)
        att = Attempt.load(exp_dir, rel, arms.get(row["arm"]), tasks[row["task"]], row)
        a0_resolved = (
            None if a0 is None or row["arm"] == "A0" else a0.get((row["task"], row["seed"]))
        )
        scores = _score_with_probe(att, params, price, a0_resolved, prober)
        record = score_record(exp, att, scores, params, prober.key if prober else None)
        (att.dir / "scores.json").write_text(json.dumps(record, indent=2))
        records.append(record)
        if tracker is not None and row.get("mlflow_run_id"):
            tracker.log_metrics(row["mlflow_run_id"], numeric_scores(scores))
    path = write_scores(exp_dir, records)
    if tracker is not None:
        for name, means in arm_means(records, _MLFLOW_MEANS).items():
            metrics = {f"score_mean_{k}": v for k, v in means.items() if v is not None}
            tracker.log_arm_summary(name, hashes[name], {}, metrics)
    echo(f"wrote {len(records)} score record(s) to {path}")
    return records


# Arm means logged to MLflow: the report's diagnostics plus the penalised rewards.
_MLFLOW_MEANS = (*DIAGNOSTIC_KEYS, "resolve", "cost_penalised", "exposure_penalised",
                 "consult_refusals", "consults_on_a0_solved", "role_map_leaks",
                 "advisor_prompt_tokens", "probe_file_top1", "probe_function_top1")  # fmt: skip


def _score_with_probe(
    att: Attempt,
    params: Params,
    price: Price | None,
    a0_resolved: bool | None,
    prober: Prober | None,
) -> dict[str, Any]:
    """Score an attempt; a probe that fails leaves its scores out and says why."""
    if prober is None:
        return score_attempt(att, params, price, a0_resolved)
    try:
        with prober.attempt(att.dir) as ask:
            return score_attempt(att, params, price, a0_resolved, ask)
    except MeteringError as e:
        scores = score_attempt(att, params, price, a0_resolved)
        return scores | {"probe_error": str(e)}
