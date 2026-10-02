from __future__ import annotations

import time
from pathlib import Path

from llm_second_opinion.adapters.base import Layer, parse_options, workspace_diff
from llm_second_opinion.config import AgentSpec, Limits
from llm_second_opinion.contracts import AgentInfo, AgentResult, ExitReason, RunConfig, Strict
from llm_second_opinion.runtime import Container
from llm_second_opinion.tasks import Task
from llm_second_opinion.tracing import Span


class GoldOptions(Strict):
    pass


class GoldAdapter:
    """Applies the task's reference patch: the upper bound, and a harness check with no model."""

    name = "gold"
    version = "1"
    capabilities: frozenset[str] = frozenset()
    uses_models = False
    artifacts: tuple[str, ...] = ()

    def __init__(self, spec: AgentSpec):
        parse_options(GoldOptions, spec)

    def build_layer(self, task_image: str) -> Layer:
        return Layer(task_image)

    def spans(self, item_dir: Path) -> list[Span]:
        return []

    def run(
        self, box: Container, task: Task, config: RunConfig, limits: Limits, env: dict[str, str]
    ) -> AgentResult:
        start = time.monotonic()
        box.write("/tmp/gold.patch", task.gold_patch)
        applied = box.exec("git apply /tmp/gold.patch", workdir=task.workdir)
        crashed = applied.exit_code != 0
        return AgentResult(
            diff="" if crashed else workspace_diff(box, task.workdir),
            exit_reason=ExitReason.CRASH if crashed else ExitReason.FINISHED,
            agent=AgentInfo(name=self.name, version=self.version),
            turns=0 if crashed else 1,
            duration_s=time.monotonic() - start,
            detail=applied.output if crashed else None,
        )
