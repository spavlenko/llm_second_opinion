from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from llm_second_opinion.config import AgentSpec, ConfigError, Limits
from llm_second_opinion.contracts import AgentResult, RunConfig
from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task
from llm_second_opinion.tracing import Span


class AgentInfraError(RuntimeError):
    """The agent stopped because of infrastructure, not its own failing: e.g. its model
    endpoint failed after the agent's own retries. The runner retries the item and, when
    retries run out, records it `failed`, not done. `result` is what the agent left (saved
    with the attempt for reading)."""

    def __init__(self, message: str, result: AgentResult | None = None):
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class Layer:
    """What the agent runs in: an image, and volumes mounted read-only (name -> path)."""

    image: str
    volumes: dict[str, str] = field(default_factory=dict)


class AgentAdapter(Protocol):
    """Runs one agent inside a task container.

    Adapters are built from an experiment's `agents` entry (`AgentSpec`). One instance per
    arm is shared by all parallel workers, so `run` must not keep state on the instance.
    """

    name: str
    version: str
    capabilities: frozenset[str]  # e.g. {"advisor", "otel"}
    # Whether the agent calls models. If so, the runner meters it: the run config points at
    # the metering proxy, and `env` holds no API keys (the proxy adds them).
    uses_models: bool
    # Files the agent leaves in the container that the runner copies into the item directory.
    artifacts: tuple[str, ...]

    def build_layer(self, task_image: str) -> Layer:
        """The task image plus the agent and its plugin; called for every item, so cache."""
        ...

    def fingerprint(self) -> dict[str, str]:
        """What the adapter runs that the experiment config does not name (its fixed task
        prompt, the agent bundle's image ID): part of the arm's config hash."""
        ...

    def provenance(self) -> dict[str, Any]:
        """What ran, for each attempt's item.json (e.g. the bundle image ID)."""
        ...

    def run(
        self, box: Container, task: Task, config: RunConfig, limits: Limits, env: dict[str, str]
    ) -> AgentResult:
        """Work on the task inside `box` and return the final diff.

        The runner has already written `config` to /run/advisor.json; `env` holds the API keys
        of an agent that is not metered (and none for one that is). Raises AgentInfraError
        when the run ended because of infrastructure rather than the agent.
        """
        ...

    def spans(self, item_dir: Path) -> list[Span]:
        """The agent's part of the item's trace, from the artifacts in `item_dir`."""
        ...

    def metrics(self, item_dir: Path) -> dict[str, Any]:
        """Trajectory metrics from the agent's own logs in `item_dir` (turns, tool calls,
        test runs, first edit, ...); see `metrics.py` for those every agent gets."""
        ...


def prompt_hash(text: str) -> str:
    """16 hex chars of SHA-256, as for prompt sets."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


M = TypeVar("M", bound=BaseModel)


def parse_options(model: type[M], spec: AgentSpec) -> M:
    """The spec's options as the adapter's options model; a typo fails before anything runs."""
    try:
        return model.model_validate(spec.options)
    except ValidationError as e:
        raise ConfigError(f"agent {spec.adapter!r} options: {e}") from e


def workspace_diff(box: Container, workdir: str) -> str:
    """Everything the agent changed, including new files."""
    result = box.exec("git add -A && git diff --cached --binary", workdir=workdir)
    if result.exit_code != 0:
        raise RuntimeError(f"git diff failed in {workdir}: {result.output}")
    return result.output
