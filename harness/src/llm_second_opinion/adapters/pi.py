"""The pi coding agent (github.com/earendil-works/pi), run non-interactively in the task container.

The agent layer is the task image, unchanged, with `/opt/lso-agent` mounted read-only from a
Docker volume filled from the pi bundle image (`agents/pi/`): Node, a pinned pi, and the
harness's pi extensions, including the advisor plugin bundled from `plugin/`. A volume rather
than an image per task: the bundle is ~560 MB, and copied into each task image it is stored
once per task. pi runs in JSON mode against one OpenAI-compatible provider, `lso`, configured
from the run's executor endpoint. The turn limit is enforced by `agents/pi/limits.ts`; the
wall-clock limit by `timeout`. Advisor arms also load the plugin, which reads
/run/advisor.json and appends to the run's events.jsonl.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from llm_second_opinion.adapters.base import (
    AgentInfraError,
    Layer,
    parse_options,
    prompt_hash,
    workspace_diff,
)
from llm_second_opinion.config import AgentSpec, Limits
from llm_second_opinion.contracts import (
    AgentInfo,
    AgentResult,
    ExitReason,
    ModelEndpoint,
    RunConfig,
    Strict,
)
from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task
from llm_second_opinion.tracing import Span, advisor_spans, clip, ms_to_ns, nest

DEFAULT_VERSION = "0.99.1"
REPO = Path(__file__).resolve().parents[4]
BUNDLE_DIR = REPO / "agents/pi"
# The advisor plugin's sources, bundled into one extension file when the bundle image is built.
PLUGIN_CONTEXTS = {
    "advisor-core": REPO / "plugin/advisor-core/src",
    "pi-binding": REPO / "plugin/pi-binding/src",
    "plugin-scripts": REPO / "plugin/scripts",
}
BUNDLE_IMAGE = "llm-second-opinion/pi-bundle"
BUNDLE_VOLUME = "llm-second-opinion-pi"  # + the bundle image's short ID
MOUNT = "/opt/lso-agent"

RUN_DIR = "/run/lso"
PI = f"{MOUNT}/bin/pi"
EXTENSIONS = (f"{MOUNT}/extensions/limits.ts", f"{MOUNT}/extensions/timeline.ts")
ADVISOR_EXTENSION = f"{MOUNT}/extensions/lso-advisor.js"
ADVISOR_CONFIG = "/run/advisor.json"
PROVIDER = "lso"

# Until prompt slots exist (docs/spec.md, research design), the executor's instructions.
PROMPT = """\
Resolve the issue below in the repository at {workdir} (your working directory).

Change the source code so the issue is fixed. Keep the change focused, and do not change
existing tests. The project is configured and built in /build; `/opt/lso/run-tests` rebuilds
it and runs the test suite (a few minutes). Your working tree is the result: when you are
done, stop.

<issue>
{problem_statement}
</issue>
"""

Thinking = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class PiOptions(Strict):
    thinking: Thinking | None = None
    tools: list[str] | None = None  # pi's defaults: read, bash, edit, write
    context_window: int | None = None  # tokens; tells pi when to compact
    max_output_tokens: int | None = None
    # pi's thinking level -> the endpoint's reasoning_effort value (None: not sent)
    thinking_level_map: dict[Thinking, str | None] | None = None
    compat: dict[str, Any] | None = None  # pi's `compat` flags for the endpoint, as is


class PiAdapter:
    name = "pi"
    capabilities: frozenset[str] = frozenset({"advisor"})
    uses_models = True
    artifacts = (
        f"{RUN_DIR}/agent/models.json",
        f"{RUN_DIR}/prompt.md",
        f"{RUN_DIR}/pi.jsonl",
        f"{RUN_DIR}/pi.stderr",
        f"{RUN_DIR}/timeline.jsonl",
        f"{RUN_DIR}/advice.jsonl",  # the plugin's full record of each consult (advisor arms)
    )

    def __init__(self, spec: AgentSpec):
        self.version = spec.version or DEFAULT_VERSION
        self.options = parse_options(PiOptions, spec)
        self._lock = threading.Lock()
        self._image_id: str | None = None
        self._volume_name: str | None = None

    # --- image -----------------------------------------------------------------------

    def build_layer(self, task_image: str) -> Layer:
        return Layer(task_image, {self._volume(): MOUNT})

    def bundle_image(self) -> str:
        """The bundle image's ID, built once per process (Docker caches the build; without
        provenance, an unchanged bundle keeps its ID). The ID this build printed, not the
        tag's: another checkout may retag meanwhile."""
        with self._lock:
            if self._image_id is None:
                tag = f"{BUNDLE_IMAGE}:{self.version}"
                contexts = [
                    a for k, v in PLUGIN_CONTEXTS.items() for a in ("--build-context", f"{k}={v}")
                ]
                self._image_id = _docker("build", "-q", "--provenance=false", "--build-arg",
                        f"PI_VERSION={self.version}", *contexts, "-t", tag, str(BUNDLE_DIR)).strip()  # fmt: skip
            return self._image_id

    def fingerprint(self) -> dict[str, str]:
        """In the config hash: the fixed task prompt, and the bundle image (pi, the harness's
        extensions, and the advisor plugin as built)."""
        return {"prompt": prompt_hash(PROMPT), "bundle_image": self.bundle_image()}

    def provenance(self) -> dict[str, Any]:
        return {
            "bundle_image": self.bundle_image(),
            "pi_version": self.version,
            "task_prompt_hash": prompt_hash(PROMPT),
        }

    def _volume(self) -> str:
        """The bundle volume, filled once per process; reused while the bundle image is
        unchanged."""
        image = self.bundle_image()
        with self._lock:
            if self._volume_name is None:
                name = f"{BUNDLE_VOLUME}-{image.removeprefix('sha256:')[:12]}"
                # One fill per volume: arms share the volume (a lock per process), and so may
                # other `bench` processes (flock inside the container). The marker is written
                # last, so an interrupted fill is redone.
                fill = (
                    f"exec flock /v/.fill.lock sh -c 'test -f /v/.complete || "
                    f"{{ rm -rf /v/* && cp -a {MOUNT}/. /v/ && touch /v/.complete; }}'"
                )
                with _fill_lock(name):
                    _docker("run", "--rm", "-v", f"{name}:/v", image, "sh", "-c", fill)
                self._volume_name = name
            return self._volume_name

    # --- run -------------------------------------------------------------------------

    def run(
        self, box: Container, task: Task, config: RunConfig, limits: Limits, env: dict[str, str]
    ) -> AgentResult:
        start = time.monotonic()
        prompt = PROMPT.format(workdir=task.workdir, problem_statement=task.problem_statement)
        box.write(f"{RUN_DIR}/prompt.md", prompt)
        box.write(f"{RUN_DIR}/agent/models.json", json.dumps(self.models_json(config.executor)))
        exit_file = f"{RUN_DIR}/exit.json"
        pi_env = {
            **env,
            "PI_CODING_AGENT_DIR": f"{RUN_DIR}/agent",
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "LSO_MAX_TURNS": str(limits.max_turns),
            "LSO_EXIT_FILE": exit_file,
            "LSO_TIMELINE": f"{RUN_DIR}/timeline.jsonl",
        }
        advisor = config.advisor is not None and config.advisor_model is not None
        if advisor:
            pi_env |= {
                "LSO_ADVISOR_CONFIG": ADVISOR_CONFIG,
                "LSO_ADVICE_LOG": f"{RUN_DIR}/advice.jsonl",
                # advisor.json keeps the endpoint as configured; the plugin reaches a host-local
                # one through the host gateway, as pi does the executor.
                "LSO_ADVISOR_BASE_URL": container_url(config.advisor_model.base_url),
            }
        done = box.exec(
            self.command(config.executor, advisor=advisor),
            workdir=task.workdir,
            timeout_s=limits.wall_minutes * 60,
            env=pi_env,
        )
        events = parse_jsonl(box.read(f"{RUN_DIR}/pi.jsonl") or "")
        stderr = box.read(f"{RUN_DIR}/pi.stderr") or ""
        limit = json.loads(box.read(exit_file) or "{}")
        reason, detail = exit_reason(done.timed_out, done.exit_code, limit, events, stderr)
        # At the limit, pi has already started (and aborted) the next turn; count the
        # completed ones, as the extension did.
        turns = limit.get("turns") or sum(e.get("type") == "turn_end" for e in events)
        result = AgentResult(
            diff=workspace_diff(box, task.workdir),
            exit_reason=reason,
            agent=AgentInfo(name=self.name, version=self.version),
            turns=turns,
            duration_s=time.monotonic() - start,
            detail=detail,
        )
        outage = endpoint_failure(events) if reason == ExitReason.CRASH else None
        if outage:
            raise AgentInfraError(outage, result)
        return result

    def command(self, executor: ModelEndpoint, advisor: bool = False) -> str:
        """pi in JSON mode with only the harness's extensions (and the advisor plugin on advisor
        arms); the prompt is read from a file."""
        thinking = self.options.thinking or executor.reasoning_effort
        extensions = [*EXTENSIONS, ADVISOR_EXTENSION] if advisor else EXTENSIONS
        args = [
            PI, "--mode", "json", "--provider", PROVIDER, "--model", executor.model,
            "--no-extensions", *[a for e in extensions for a in ("-e", e)],
            "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
            "--offline", "--session-dir", f"{RUN_DIR}/sessions",
        ]  # fmt: skip
        if thinking:
            args += ["--thinking", thinking]
        if self.options.tools is not None:
            args += ["--tools", ",".join(self.options.tools)]
        # `exec` so `timeout` signals pi itself, not a wrapping shell.
        return (
            f'exec {shlex.join(args)} -- "$(cat {RUN_DIR}/prompt.md)" '
            f"> {RUN_DIR}/pi.jsonl 2> {RUN_DIR}/pi.stderr"
        )

    def models_json(self, executor: ModelEndpoint) -> dict:
        """pi's models.json: the executor endpoint as the `lso` provider. The API key and
        secret headers are referenced by variable name and read from the per-exec
        environment, so the file holds no secrets."""
        opts = self.options
        model: dict = {"id": executor.model, "name": executor.model}
        if opts.thinking or executor.reasoning_effort or opts.thinking_level_map:
            model["reasoning"] = True
        if opts.thinking_level_map:
            model["thinkingLevelMap"] = opts.thinking_level_map
        if opts.context_window:
            model["contextWindow"] = opts.context_window
        if opts.max_output_tokens:
            model["maxTokens"] = opts.max_output_tokens
        if opts.compat:
            model["compat"] = opts.compat
        # pi merges samplingParams into every request body as is (last, so they win).
        sampling = {
            "temperature": executor.temperature,
            "top_p": executor.top_p,
            "seed": executor.sampling_seed,
        }
        sampling = {k: v for k, v in sampling.items() if v is not None}
        if sampling:
            model["samplingParams"] = sampling
        provider: dict = {
            "baseUrl": container_url(executor.base_url),
            "api": "openai-completions",
            "apiKey": f"${{{executor.api_key_env}}}" if executor.api_key_env else "none",
            "models": [model],
        }
        headers = executor.headers | {h: f"${{{v}}}" for h, v in executor.header_env.items()}
        if headers:
            provider["headers"] = headers
        return {"providers": {PROVIDER: provider}}

    def spans(self, item_dir: Path) -> list[Span]:
        """Turn spans, each with its model calls and tool calls, from the item's artifacts."""
        events_path, timeline_path = item_dir / "pi.jsonl", item_dir / "timeline.jsonl"
        if not (events_path.exists() and timeline_path.exists()):
            return []
        turns = pi_spans(
            parse_jsonl(events_path.read_text()), parse_jsonl(timeline_path.read_text())
        )
        # The advisor plugin's consults, from the run's events (the contract) and the plugin's
        # own advice log (system prompt and advice text).
        advice = {r["request_id"]: r for r in _records(item_dir / "advice.jsonl")}
        return nest(turns, advisor_spans(_records(item_dir / "events.jsonl"), advice))

    def metrics(self, item_dir: Path) -> dict[str, Any]:
        events_path = item_dir / "pi.jsonl"
        if not events_path.exists():
            return {}
        return pi_metrics(
            parse_jsonl(events_path.read_text()), _records(item_dir / "timeline.jsonl")
        )


TEST_COMMAND = "/opt/lso/run-tests"
# A failing test run when its exit status is hidden (piped through `tail`), as the plugin's
# on_test_failure trigger reads it.
_TESTS_FAILED = re.compile(
    r"\b[1-9]\d* tests failed out of|The following tests FAILED|run-tests: build failed"
)
_SHELL_EDIT = re.compile(
    r"\bsed\b[^|;&]*\s-[a-zA-Z]*i|\bperl\b[^|;&]*\s-[a-zA-Z]*i|\bgit\s+apply\b|"
    r"(^|[\s;&|(])patch\s"
)
EDIT_TOOLS = {"edit", "write"}


def pi_metrics(events: list[dict], timeline: list[dict]) -> dict[str, Any]:
    """Trajectory metrics from pi's JSON events, with times from the timeline extension
    (seconds since the first turn started; None without a timeline). Turns count from 0."""
    tool_start = {m["id"]: m["t"] for m in timeline if m.get("event") == "tool_start"}
    t0 = timeline[0]["t"] if timeline else None

    def seconds(call_id: str) -> float | None:
        t = tool_start.get(call_id)
        return None if t is None or t0 is None else round((t - t0) / 1000, 3)

    tools: Counter[str] = Counter()
    calls: dict[str, dict] = {}  # tool call id -> {name, command, turn}
    first: dict[str, dict] = {}  # edit, test -> {turn, s}
    test_runs = test_failures = compactions = retries = retry_failures = 0
    turn = -1
    for e in events:
        kind = e.get("type")
        if kind == "turn_start":
            turn += 1
        elif kind == "tool_execution_start":
            name = e.get("toolName") or "?"
            tools[name] += 1
            args = e.get("args") if isinstance(e.get("args"), dict) else {}
            command = str(args.get("command") or "") if name == "bash" else ""
            calls[e.get("toolCallId")] = {"name": name, "command": command}
            mark = {"turn": max(turn, 0), "s": seconds(e.get("toolCallId"))}
            if name in EDIT_TOOLS or _SHELL_EDIT.search(command):
                first.setdefault("edit", mark)
            if TEST_COMMAND in command:
                test_runs += 1
                first.setdefault("test", mark)
        elif kind == "tool_execution_end":
            call = calls.get(e.get("toolCallId"), {})
            if TEST_COMMAND in call.get("command", ""):
                text = _text((e.get("result") or {}).get("content"))
                test_failures += bool(e.get("isError") or _TESTS_FAILED.search(text))
        elif kind == "compaction_start":
            compactions += 1
        elif kind == "auto_retry_start":
            retries += 1
        elif kind == "auto_retry_end" and e.get("success") is False:
            retry_failures += 1
    edit, test = first.get("edit", {}), first.get("test", {})
    return {
        "turns": sum(e.get("type") == "turn_end" for e in events),
        "tool_calls": sum(tools.values()),
        "tool_calls_by_name": dict(sorted(tools.items())),
        "test_runs": test_runs,
        "test_runs_failed": test_failures,
        "compactions": compactions,
        "auto_retries": retries,
        "auto_retry_failures": retry_failures,
        "first_edit_turn": edit.get("turn"),
        "first_edit_s": edit.get("s"),
        "first_test_turn": test.get("turn"),
        "first_test_s": test.get("s"),
        # For the metrics every agent gets (events.jsonl times are Unix seconds).
        "agent_start_ts": t0 / 1000 if t0 is not None else None,
    }


def pi_spans(events: list[dict], timeline: list[dict]) -> list[Span]:
    """Pair pi's events (content) with the timeline extension's marks (times), in order."""
    marks: dict[str, list[int]] = {}
    tool_times: dict[str, list[int]] = {}
    for m in timeline:
        t = ms_to_ns(m["t"])
        if m["event"] in ("tool_start", "tool_end"):
            tool_times.setdefault(m["id"], []).append(t)
        else:
            marks.setdefault(m["event"], []).append(t)
    turns: list[Span] = []
    pending_inputs: list[dict] = []  # messages the next model call responds to
    tools: dict[str, dict] = {}
    model_calls = 0
    for e in events:
        kind = e.get("type")
        if kind == "turn_start":
            i = len(turns)
            start = _at(marks, "turn_start", i)
            turns.append(Span(f"turn {i + 1}", "CHAIN", start, _at(marks, "turn_end", i, start)))
        elif kind == "message_end" and turns:
            message = e["message"]
            role = message.get("role")
            if role in ("user", "toolResult"):
                pending_inputs.append({"role": role, "content": _text(message.get("content"))})
            elif role == "assistant":
                start = _at(marks, "model_start", model_calls, turns[-1].start_ns)
                end = _at(marks, "model_end", model_calls, start)
                model_calls += 1
                turns[-1].children.append(_model_span(message, pending_inputs, start, end))
                pending_inputs = []
        elif kind == "tool_execution_start":
            tools[e["toolCallId"]] = {"name": e["toolName"], "args": e.get("args")}
        elif kind == "tool_execution_end" and turns:
            call = tools.get(e["toolCallId"], {"name": e.get("toolName"), "args": None})
            times = tool_times.get(e["toolCallId"], [])
            start = times[0] if times else turns[-1].start_ns
            end = times[1] if len(times) > 1 else start
            result = _text((e.get("result") or {}).get("content"))
            turns[-1].children.append(
                Span(
                    call["name"] or "tool",
                    "TOOL",
                    start,
                    end,
                    inputs=clip(call["args"]),
                    outputs=clip(result),
                    error=clip(result) if e.get("isError") else None,
                )
            )
    return turns


def _model_span(message: dict, inputs: list[dict], start: int, end: int) -> Span:
    blocks = message.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    thinking = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
    calls = [
        {"name": b.get("name"), "arguments": b.get("arguments")}
        for b in blocks
        if b.get("type") == "toolCall"
    ]
    usage = message.get("usage") or {}
    outputs = {"text": text, "tool_calls": calls} | ({"thinking": thinking} if thinking else {})
    return Span(
        str(message.get("model") or "model"),
        "CHAT_MODEL",
        start,
        end,
        inputs=clip(inputs),
        outputs=clip(outputs),
        attributes={
            "stop_reason": message.get("stopReason"),
            # As pi reports them; the metering proxy's counts are the source of truth.
            "tokens.input": usage.get("input", 0),
            "tokens.output": usage.get("output", 0),
            "tokens.cache_read": usage.get("cacheRead", 0),
            "tokens.total": usage.get("totalTokens", 0),
        },
        error=message.get("errorMessage") if message.get("stopReason") == "error" else None,
    )


def _records(path: Path) -> list[dict]:
    return parse_jsonl(path.read_text()) if path.exists() else []


def _at(marks: dict[str, list[int]], event: str, i: int, default: int = 0) -> int:
    times = marks.get(event, [])
    return times[i] if i < len(times) else default


def _text(content: object) -> str:
    """Message content as text: a string, or the text of its blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def exit_reason(
    timed_out: bool, exit_code: int, limit: dict, events: list[dict], stderr: str
) -> tuple[ExitReason, str | None]:
    if timed_out:
        return ExitReason.TIME_LIMIT, None
    if limit.get("reason") == "turn_limit":
        return ExitReason.TURN_LIMIT, None
    if exit_code != 0:
        return ExitReason.CRASH, f"pi exited {exit_code}: {stderr[-2000:]}".strip()
    error = last_error(events)
    if error:
        return ExitReason.CRASH, error
    return ExitReason.FINISHED, None


_HTTP_STATUS = re.compile(r"^\s*(\d{3})\b")
# Statuses about the request itself (a context too long, a malformed tool call): the agent's
# doing, so a crash. Any other status means the endpoint failed.
_REQUEST_STATUSES = {400, 413, 422}
# Transport failures as pi and its SDKs word them (cf. pi-ai's retryable error pattern).
_TRANSPORT = re.compile(
    r"connection.?(error|refused|reset|lost|closed)|ECONNREFUSED|ECONNRESET|ETIMEDOUT|"
    r"ENOTFOUND|EAI_AGAIN|getaddrinfo|fetch failed|socket hang up|other side closed|"
    r"network.?error|timed? out|overloaded|service.?unavailable|rate.?limit|"
    r"too many requests|upstream.?connect|reset before headers",
    re.IGNORECASE,
)


def endpoint_failure(events: list[dict]) -> str | None:
    """Why the run ended on its model endpoint failing, after pi's own retries, or None if it
    did not: pi gave up retrying (`auto_retry_end` with `success: false`), or the final
    assistant message ended in an HTTP error (other than one about the request itself) or a
    connection error. The proxy's token-budget refusal is not a failure: the runner records
    `token_limit`."""
    for e in events:
        if e.get("type") == "auto_retry_end" and e.get("success") is False:
            error = str(e.get("finalError") or "")
            if "token_limit" not in error:
                return f"model endpoint failed after {e.get('attempt')} attempt(s): {error}"
    error = last_error(events)
    if not error or "token_limit" in error:
        return None
    status = _HTTP_STATUS.match(error)
    if status:
        return None if int(status[1]) in _REQUEST_STATUSES else f"model endpoint failed: {error}"
    return f"model endpoint failed: {error}" if _TRANSPORT.search(error) else None


def last_error(events: list[dict]) -> str | None:
    """The error of the final assistant message, if the run ended on one (e.g. the model
    endpoint failed after pi's retries)."""
    for e in reversed(events):
        message = e.get("message") if e.get("type") == "message_end" else None
        if message and message.get("role") == "assistant":
            if message.get("stopReason") == "error":
                return str(message.get("errorMessage") or "model error")
            return None
    return None


def parse_jsonl(text: str) -> list[dict]:
    """pi's JSON-mode records; split on LF only, as pi's docs require."""
    records = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a record cut off by a kill
    return records


def container_url(url: str) -> str:
    """A host-local URL as containers reach it (Docker's host-gateway alias)."""
    parts = urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


_FILL_LOCKS: dict[str, threading.Lock] = {}
_FILL_LOCKS_GUARD = threading.Lock()


def _fill_lock(volume: str) -> threading.Lock:
    """The process-wide lock for filling one bundle volume (shared by every arm's adapter)."""
    with _FILL_LOCKS_GUARD:
        return _FILL_LOCKS.setdefault(volume, threading.Lock())


def _docker(*args: str, input: str | None = None) -> str:
    done = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()[-2000:]}")
    return done.stdout
