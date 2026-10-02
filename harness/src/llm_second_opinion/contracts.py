"""The four versioned contracts shared by the harness, the plugin, and the metering proxy.

These Pydantic models are the single source: `bench schemas` writes them to
`schemas/` as JSON Schemas, and the plugin's TypeScript types are generated from
those files. Bump SCHEMA_VERSION on any breaking change.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

SCHEMA_VERSION = "1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Level(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class Intervention(StrEnum):
    PLAN = "plan"  # harness: review the executor's plan at the start
    CONSULT = "consult"  # executor: the consult tool
    STUCK = "stuck"  # harness: the stuck heuristic
    ON_TEST_FAILURE = "on_test_failure"  # harness: after a failed test run
    PERIODIC = "periodic"  # harness: every `periodic_every` turns


class PromptSlot(StrEnum):
    EXECUTOR_GUIDANCE = "executor_guidance"  # appended to the executor's system prompt
    CONSULT_TOOL = "consult_tool"  # the consult tool's description
    BRIEF = "brief"  # the brief builder's template
    ADVISOR_SYSTEM = "advisor_system"  # the advisor's system prompt
    ADVICE_INJECTION = "advice_injection"  # how advice re-enters the executor's context


# --- Run config (in): advisor.json -------------------------------------------


class ModelEndpoint(Strict):
    base_url: str
    model: str
    reasoning_effort: str | None = None
    api_key_env: str | None = Field(
        default=None,
        description="Name of the environment variable holding the API key. Never the key itself.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="Extra request headers with non-secret values."
    )
    header_env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra request headers with secret values: header name to the name of the "
        "environment variable holding the value. Never the value itself.",
    )


class StuckThresholds(Strict):
    repeat_calls: int = Field(default=3, ge=1)
    same_error: int = Field(default=3, ge=1)
    no_diff_turns: int = Field(default=8, ge=1)


class AdvisorSettings(Strict):
    level: Level
    interventions: list[Intervention] = Field(
        default_factory=lambda: [Intervention.PLAN, Intervention.CONSULT, Intervention.STUCK]
    )
    prompts: str = Field(
        default="default", description="Name of a prompt set in the experiment's `prompts`."
    )
    max_consults: int = Field(default=5, ge=0)
    max_answer_tokens: int | None = Field(
        default=None, ge=1, description="Cap on each advisor answer; None means no cap."
    )
    cooldown_turns: int = Field(
        default=0, ge=0, description="Turns after a consult before a harness trigger may fire."
    )
    periodic_every: int | None = Field(
        default=None, ge=1, description="Turns between `periodic` triggers."
    )
    stuck: StuckThresholds = Field(default_factory=StuckThresholds)

    @model_validator(mode="after")
    def _periodic_needs_interval(self) -> AdvisorSettings:
        if Intervention.PERIODIC in self.interventions and self.periodic_every is None:
            raise ValueError("the periodic intervention needs periodic_every")
        return self


class PromptTexts(Strict):
    """The text of every prompt slot, with `{{name}}` placeholders still in place."""

    executor_guidance: str
    consult_tool: str
    brief: str
    advisor_system: str
    advice_injection: str


class PromptSet(Strict):
    name: str
    hash: str = Field(description="16 hex chars of SHA-256 over the slot texts.")
    texts: PromptTexts


class RunIdentity(Strict):
    experiment: str
    arm: str
    task: str
    seed: int
    config_hash: str


class RunConfig(Strict):
    """Written by the harness into the container as advisor.json."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    run: RunIdentity
    executor: ModelEndpoint
    advisor_model: ModelEndpoint | None = None
    advisor: AdvisorSettings | None = Field(
        default=None, description="Null when the arm runs without advisor interventions."
    )
    prompts: PromptSet | None = Field(
        default=None, description="The resolved prompt set of an advisor arm; null otherwise."
    )
    events_path: str = "/run/events.jsonl"


# --- Events (out): events.jsonl ----------------------------------------------


class _Event(Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    seq: int = Field(ge=0, description="Monotonic per run, starting at 0.")
    ts: float = Field(description="Unix time in seconds.")


class ConsultRequested(_Event):
    """The executor called the consult tool."""

    type: Literal["consult_requested"] = "consult_requested"
    reason: str = Field(description="The executor's stated reason, as it wrote it.")
    turn: int = Field(ge=0)


class TriggerFired(_Event):
    type: Literal["trigger_fired"] = "trigger_fired"
    intervention: Intervention
    reason: str
    turn: int = Field(ge=0)


class BriefBuilt(_Event):
    type: Literal["brief_built"] = "brief_built"
    level: Level
    tokens: int = Field(ge=0)
    identifiers_redacted: int = Field(ge=0)
    role_map_size: int = Field(ge=0)


class AdvisorRequest(_Event):
    type: Literal["advisor_request"] = "advisor_request"
    request_id: str
    input_tokens: int = Field(ge=0)
    brief_text: str = Field(description="The exact text sent to the advisor.")
    prompt_hash: str = Field(description="Hash of the prompt set that produced the request.")


class AdvisorResponse(_Event):
    type: Literal["advisor_response"] = "advisor_response"
    request_id: str
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(ge=0)


class AdvisorError(_Event):
    type: Literal["advisor_error"] = "advisor_error"
    request_id: str
    message: str


class AdviceApplied(_Event):
    type: Literal["advice_applied"] = "advice_applied"
    request_id: str
    turn: int = Field(ge=0)


class BudgetExhausted(_Event):
    type: Literal["budget_exhausted"] = "budget_exhausted"
    consults_used: int = Field(ge=0)
    limit: int = Field(ge=0)


Event = Annotated[
    ConsultRequested
    | TriggerFired
    | BriefBuilt
    | AdvisorRequest
    | AdvisorResponse
    | AdvisorError
    | AdviceApplied
    | BudgetExhausted,
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def parse_events(lines: str) -> list[Event]:
    """Parse the contents of an events.jsonl file, skipping blank lines."""
    return [EVENT_ADAPTER.validate_json(line) for line in lines.splitlines() if line.strip()]


# --- Result (out) ------------------------------------------------------------


class ExitReason(StrEnum):
    FINISHED = "finished"
    TURN_LIMIT = "turn_limit"
    TIME_LIMIT = "time_limit"
    TOKEN_LIMIT = "token_limit"
    CRASH = "crash"


class AgentInfo(Strict):
    name: str
    version: str


class AgentResult(Strict):
    """Returned by an AgentAdapter after a run."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    diff: str = Field(description="Final `git diff` of the workspace.")
    exit_reason: ExitReason
    agent: AgentInfo
    turns: int = Field(ge=0)
    duration_s: float = Field(ge=0)
    detail: str | None = Field(default=None, description="Error text when exit_reason is crash.")
    usage: list[RoleUsage] = Field(
        default_factory=list,
        description="Per-role totals from the metering proxy; empty when the run was not metered.",
    )


# --- Usage (out): usage.jsonl, written by the metering proxy -------------------


Role = Literal["executor", "advisor"]


class UsageRecord(Strict):
    """One model call through the metering proxy. Counts are the provider's own `usage`."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    seq: int = Field(ge=0, description="Call order within the item, from 0.")
    ts: float = Field(description="Unix time in seconds when the call started.")
    role: Role
    model: str = Field(description="Model id as sent.")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(ge=0)
    status: int = Field(description="HTTP status from the upstream endpoint.")


class RoleUsage(Strict):
    role: Role
    calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)


# --- Schema export -------------------------------------------------------------

SCHEMAS: dict[str, tuple[str, TypeAdapter]] = {
    "run-config.schema.json": ("RunConfig", TypeAdapter(RunConfig)),
    "event.schema.json": ("Event", EVENT_ADAPTER),
    "result.schema.json": ("AgentResult", TypeAdapter(AgentResult)),
    "usage.schema.json": ("UsageRecord", TypeAdapter(UsageRecord)),
}


def _normalize(node: Any) -> Any:
    """Make every property required and drop per-field titles.

    Dumps always write every field (optionals as null), and the `type` discriminator must
    be required for a TypeScript union. Pydantic's field titles would each become a
    pointless named type in the generated TypeScript.
    """
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: _normalize(v) for k, v in node.items()}
    if isinstance(node.get("properties"), dict):
        node["properties"] = {
            name: {k: v for k, v in field.items() if k != "title"}
            for name, field in node["properties"].items()
        }
        node["required"] = list(node["properties"])
    return node


def render_schemas() -> dict[str, str]:
    """Return {file name: JSON text} for every contract."""
    out = {}
    for name, (title, adapter) in SCHEMAS.items():
        schema = _normalize(adapter.json_schema(mode="serialization"))
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": title,
            **schema,
        }
        out[name] = json.dumps(schema, indent=2) + "\n"
    return out


def stale_schemas(directory: Path) -> list[str]:
    """Names of schema files that are missing or differ from the models."""
    return [
        name
        for name, text in render_schemas().items()
        if not (directory / name).exists() or (directory / name).read_text() != text
    ]
