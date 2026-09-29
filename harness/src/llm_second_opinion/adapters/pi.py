"""The pi coding agent (github.com/earendil-works/pi), run non-interactively in the task container.

The agent layer is the task image, unchanged, with `/opt/lso-agent` mounted read-only from a
Docker volume filled from the pi bundle image (`agents/pi/`): Node, a pinned pi, and the
harness's pi extensions. A volume rather than an image per task: the bundle is ~560 MB, and
copied into each task image it is stored once per task. pi runs in JSON mode
against one OpenAI-compatible provider, `lso`, configured from the run's executor endpoint.
The turn limit is enforced by `agents/pi/limits.ts`; the wall-clock limit by `timeout`.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from llm_second_opinion.adapters.base import Layer, parse_options, workspace_diff
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
from llm_second_opinion.tracing import Span, clip, ms_to_ns

DEFAULT_VERSION = "0.99.1"
BUNDLE_DIR = Path(__file__).resolve().parents[4] / "agents/pi"
BUNDLE_IMAGE = "llm-second-opinion/pi-bundle"
BUNDLE_VOLUME = "llm-second-opinion-pi"  # + the bundle image's short ID
MOUNT = "/opt/lso-agent"

RUN_DIR = "/run/lso"
PI = f"{MOUNT}/bin/pi"
EXTENSIONS = (f"{MOUNT}/extensions/limits.ts", f"{MOUNT}/extensions/timeline.ts")
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


class PiAdapter:
    name = "pi"
    capabilities: frozenset[str] = frozenset()  # "advisor" once pi-binding is in the layer
    artifacts = (
        f"{RUN_DIR}/prompt.md",
        f"{RUN_DIR}/pi.jsonl",
        f"{RUN_DIR}/pi.stderr",
        f"{RUN_DIR}/timeline.jsonl",
    )

    def __init__(self, spec: AgentSpec):
        self.version = spec.version or DEFAULT_VERSION
        self.options = parse_options(PiOptions, spec)
        self._lock = threading.Lock()
        self._volume_name: str | None = None

    # --- image -----------------------------------------------------------------------

    def build_layer(self, task_image: str) -> Layer:
        return Layer(task_image, {self._volume(): MOUNT})

    def _volume(self) -> str:
        """The bundle volume, built and filled once per process (Docker caches the image
        build; a volume is reused while the bundle image is unchanged)."""
        with self._lock:
            if self._volume_name is None:
                tag = f"{BUNDLE_IMAGE}:{self.version}"
                # Without provenance, an unchanged bundle keeps its image ID (and volume).
                _docker("build", "-q", "--provenance=false", "--build-arg",
                        f"PI_VERSION={self.version}", "-t", tag, str(BUNDLE_DIR))  # fmt: skip
                name = f"{BUNDLE_VOLUME}-{_image_id(tag)[7:19]}"
                # The marker is written last, so an interrupted fill is redone.
                fill = (
                    f"test -f /v/.complete || {{ rm -rf /v/* && cp -a {MOUNT}/. /v/ "
                    "&& touch /v/.complete; }"
                )
                _docker("run", "--rm", "-v", f"{name}:/v", tag, "sh", "-c", fill)
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
        done = box.exec(
            self.command(config.executor),
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
        return AgentResult(
            diff=workspace_diff(box, task.workdir),
            exit_reason=reason,
            agent=AgentInfo(name=self.name, version=self.version),
            turns=turns,
            duration_s=time.monotonic() - start,
            detail=detail,
        )

    def command(self, executor: ModelEndpoint) -> str:
        """pi in JSON mode with only the harness's extensions; the prompt is read from a file."""
        thinking = self.options.thinking or executor.reasoning_effort
        args = [
            PI, "--mode", "json", "--provider", PROVIDER, "--model", executor.model,
            "--no-extensions", *[a for e in EXTENSIONS for a in ("-e", e)],
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
        """pi's models.json: the executor endpoint as the `lso` provider. The API key is
        referenced by variable name and read from the per-exec environment."""
        model: dict = {"id": executor.model, "name": executor.model}
        if self.options.thinking or executor.reasoning_effort:
            model["reasoning"] = True
        if self.options.context_window:
            model["contextWindow"] = self.options.context_window
        if self.options.max_output_tokens:
            model["maxTokens"] = self.options.max_output_tokens
        return {
            "providers": {
                PROVIDER: {
                    "baseUrl": container_url(executor.base_url),
                    "api": "openai-completions",
                    "apiKey": f"${{{executor.api_key_env}}}" if executor.api_key_env else "none",
                    "models": [model],
                }
            }
        }

    def spans(self, item_dir: Path) -> list[Span]:
        """Turn spans, each with its model calls and tool calls, from the item's artifacts."""
        events_path, timeline_path = item_dir / "pi.jsonl", item_dir / "timeline.jsonl"
        if not (events_path.exists() and timeline_path.exists()):
            return []
        return pi_spans(
            parse_jsonl(events_path.read_text()), parse_jsonl(timeline_path.read_text())
        )


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
            # As reported by the model server; the metering proxy will be the source of truth.
            "tokens.input": usage.get("input", 0),
            "tokens.output": usage.get("output", 0),
            "tokens.cache_read": usage.get("cacheRead", 0),
            "tokens.total": usage.get("totalTokens", 0),
        },
        error=message.get("errorMessage") if message.get("stopReason") == "error" else None,
    )


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


def _docker(*args: str, input: str | None = None) -> str:
    done = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()[-2000:]}")
    return done.stdout


def _image_id(ref: str) -> str:
    return _docker("image", "inspect", "--format", "{{.Id}}", ref).strip()
