"""Expand arms x tasks x seeds into work items and run them, in parallel, resuming via the ledger."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from llm_second_opinion import __version__
from llm_second_opinion.adapters import ADAPTERS, AgentAdapter
from llm_second_opinion.config import ADVISOR_MODEL, Arm, ConfigError, Experiment
from llm_second_opinion.contracts import AgentResult
from llm_second_opinion.grading import Grade, grade
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.runtime import Container, Runtime
from llm_second_opinion.tasks import Manifest, Task


class Tracker(Protocol):
    """Where results go besides the ledger; `tracking.Tracker` logs to MLflow."""

    def log_item(
        self,
        key: ItemKey,
        arm_params: dict[str, Any],
        params: dict[str, Any],
        metrics: dict[str, float],
        artifacts: Path,
    ) -> str:
        """Record one finished work item and return its run id."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class WorkItem:
    arm: Arm
    task: Task
    seed: int
    key: ItemKey


@dataclass
class Outcome:
    key: ItemKey
    status: Literal["done", "failed", "skipped"]
    resolved: bool | None = None


class Runner:
    """Runs an experiment's work items; `Runner(exp, Path("runs")).run()` is the whole batch.

    Items run on `parallel` worker threads (containers do the work; threads only wait on
    them). Each item is retried after an infrastructure error, and the ledger in
    `runs_dir/<experiment>/` makes a rerun skip what is already done.
    """

    def __init__(
        self,
        exp: Experiment,
        runs_dir: Path,
        *,
        runtime: Runtime | None = None,
        tracker: Tracker | None = None,
        parallel: int | None = None,
        echo: Callable[[str], None] = print,
    ):
        self.exp = exp
        self.manifest = Manifest.from_yaml(exp.tasks)
        self.dir = runs_dir / exp.name
        self.ledger = Ledger(self.dir / "ledger.sqlite")
        self._runtime = runtime
        self.tracker = tracker
        self.parallel = parallel or exp.execution.parallel
        self.echo = echo
        self.adapters = {arm.name: _adapter_for(arm) for arm in exp.arms}

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            self._runtime = Runtime()
        return self._runtime

    def items(self, arm: str | None = None, task: str | None = None) -> list[WorkItem]:
        """Seed-major order, so a partial batch covers every arm and task evenly."""
        arms = [self.exp.arm(arm)] if arm else self.exp.arms
        tasks = [t for t in self.manifest.tasks if task in (None, t.id)]
        if not tasks:
            raise ConfigError(f"no task {task!r} in {self.exp.tasks}")
        return [
            WorkItem(
                a, t, seed, ItemKey(self.exp.name, a.name, t.id, seed, self.exp.config_hash(a))
            )
            for seed in range(self.exp.seeds)
            for t in tasks
            for a in arms
        ]

    def run(self, arm: str | None = None, task: str | None = None) -> list[Outcome]:
        items = self.items(arm, task)
        todo = [i for i in items if self.ledger.status(i.key) != "done"]
        skipped = [Outcome(i.key, "skipped") for i in items if i not in todo]
        if skipped:
            self.echo(f"skipping {len(skipped)} item(s) already done")
        env = _secrets(self.exp, {i.arm.name: i.arm for i in todo}.values())
        self.echo(f"running {len(todo)} item(s), {self.parallel} at a time")
        pool = ThreadPoolExecutor(self.parallel)
        try:
            outcomes = list(pool.map(lambda i: self._run_item(i, env), todo))
        finally:
            # On Ctrl-C, items in flight finish and are recorded; queued ones are dropped and
            # run on the next resume.
            pool.shutdown(cancel_futures=True)
            if self.tracker:
                self.tracker.close()
        return skipped + outcomes

    def _run_item(self, item: WorkItem, env: dict[str, str]) -> Outcome:
        retries = self.exp.execution.retries
        while True:
            attempt = self.ledger.start(item.key)
            try:
                result, graded, run_id = self._attempt(item, env)
            # One item's infrastructure failure (Docker, git, I/O) must not stop the batch;
            # it is retried, then recorded in the ledger.
            except Exception as e:  # noqa: BLE001
                if attempt <= retries:
                    self.echo(f"{item.key}: attempt {attempt} failed ({e}); retrying")
                    continue
                self.ledger.finish(item.key, "failed", error=f"{type(e).__name__}: {e}")
                self.echo(f"{item.key}: FAILED after {attempt} attempt(s): {e}")
                return Outcome(item.key, "failed")
            self.ledger.finish(
                item.key,
                "done",
                resolved=graded.resolved,
                grade=graded.reason,
                exit_reason=result.exit_reason.value,
                turns=result.turns,
                duration_s=result.duration_s,
                mlflow_run_id=run_id,
            )
            self.echo(f"{item.key}: {result.exit_reason.value}, {graded.reason}")
            return Outcome(item.key, "done", graded.resolved)

    def _attempt(
        self, item: WorkItem, env: dict[str, str]
    ) -> tuple[AgentResult, Grade, str | None]:
        """Steps 3-7 of the run lifecycle for one item; artifacts land in the item's directory."""
        exe = self.exp.execution
        adapter = self.adapters[item.arm.name]
        config = self.exp.run_config(item.arm, item.task.id, item.seed)
        out = self.dir / item.arm.name / item.task.id / f"seed-{item.seed}"
        out.mkdir(parents=True, exist_ok=True)
        rendered = config.model_dump_json(indent=2)
        (out / "advisor.json").write_text(rendered)

        with self._container(adapter.build_layer(item.task.image), f"agent {item.key}") as box:
            box.write("/run/advisor.json", rendered)
            result = adapter.run(box, item.task, config, self.exp.limits, env)
            events = box.read(config.events_path) or ""
        (out / "result.json").write_text(result.model_dump_json(indent=2))
        (out / "patch.diff").write_text(result.diff)
        (out / "events.jsonl").write_text(events)

        with self._container(item.task.image, f"grade {item.key}") as box:
            graded = grade(box, item.task, result.diff, exe.grade_minutes * 60)
        (out / "grade.log").write_text(graded.log)

        run_id = None
        if self.tracker:
            run_id = self.tracker.log_item(
                item.key,
                arm_params=self._pinned_inputs(item.arm, adapter),
                params={
                    "task": item.task.id,
                    "seed": item.seed,
                    "grade": graded.reason,
                    "exit_reason": result.exit_reason.value,
                },
                metrics={
                    "resolved": float(graded.resolved),
                    "turns": result.turns,
                    "duration_s": result.duration_s,
                },
                artifacts=out,
            )
        return result, graded, run_id

    @contextmanager
    def _container(self, image: str, name: str) -> Iterator[Container]:
        """A capped container that is removed however the block exits."""
        exe = self.exp.execution
        box = self.runtime.start(image, exe.cpus, exe.memory_gb, name)
        try:
            yield box
        finally:
            box.remove()

    def _pinned_inputs(self, arm: Arm, adapter: AgentAdapter) -> dict[str, Any]:
        """Everything a reader needs to reproduce the arm, logged once per arm run."""
        executor = self.exp.models[arm.executor]
        advisor = self.exp.models[ADVISOR_MODEL] if arm.advisor else None
        return {
            "config_hash": self.exp.config_hash(arm),
            "manifest_version": self.manifest.version,
            "harness_version": __version__,
            "agent": adapter.name,
            "executor_model": executor.model,
            "executor_reasoning_effort": executor.reasoning_effort,
            "advisor_model": advisor.model if advisor else None,
            "advisor_reasoning_effort": advisor.reasoning_effort if advisor else None,
            "advisor_level": arm.advisor.level if arm.advisor else None,
            "max_turns": self.exp.limits.max_turns,
            "wall_minutes": self.exp.limits.wall_minutes,
        }


def _adapter_for(arm: Arm) -> AgentAdapter:
    if arm.agent not in ADAPTERS:
        raise ConfigError(
            f"arm {arm.name}: unknown agent {arm.agent!r}; known: {', '.join(ADAPTERS)}"
        )
    adapter = ADAPTERS[arm.agent]()
    if arm.advisor and "advisor" not in adapter.capabilities:
        raise ConfigError(f"arm {arm.name}: agent {arm.agent!r} does not support an advisor")
    return adapter


def _secrets(exp: Experiment, arms: Iterable[Arm]) -> dict[str, str]:
    """API keys the arms' models name, read from the host environment. Fails before any run."""
    names = set()
    for arm in arms:
        models = [exp.models[arm.executor]] + ([exp.models[ADVISOR_MODEL]] if arm.advisor else [])
        names |= {m.api_key_env for m in models if m.api_key_env}
    missing = sorted(n for n in names if n not in os.environ)
    if missing:
        raise ConfigError(f"unset API key variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
