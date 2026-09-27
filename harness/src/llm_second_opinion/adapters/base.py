from __future__ import annotations

from typing import Protocol

from llm_second_opinion.config import Limits
from llm_second_opinion.contracts import AgentResult, RunConfig
from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task


class AgentAdapter(Protocol):
    """Runs one agent inside a task container.

    One instance per arm is shared by all parallel workers, so `run` must not keep state
    on the instance.
    """

    name: str
    capabilities: frozenset[str]  # e.g. {"advisor", "otel"}

    def build_layer(self, task_image: str) -> str:
        """The image the agent runs in: the task image plus the agent and its plugin."""
        ...

    def run(
        self, box: Container, task: Task, config: RunConfig, limits: Limits, env: dict[str, str]
    ) -> AgentResult:
        """Work on the task inside `box` and return the final diff.

        The runner has already written `config` to /run/advisor.json; `env` holds the API keys.
        """
        ...


def workspace_diff(box: Container, workdir: str) -> str:
    """Everything the agent changed, including new files."""
    result = box.exec("git add -A && git diff --cached --binary", workdir=workdir)
    if result.exit_code != 0:
        raise RuntimeError(f"git diff failed in {workdir}: {result.output}")
    return result.output
