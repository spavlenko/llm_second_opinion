"""Expand arms x tasks x seeds into work items and run them, in parallel, resuming via the ledger.

Every attempt at an item gets its own directory, `runs/<experiment>/<arm>/<task>/seed-<n>/
<config_hash>/attempt-<k>/`, which starts empty and is never reused, and its own row in the
ledger's `attempts` table (with what it spent, whatever its outcome). An attempt runs in
phases: the agent (its result saved to disk), grading, then tracking. If grading or tracking
fails, only that phase is retried, from the result on disk, also by a later `bench run`.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Literal, Protocol

from llm_second_opinion import __version__
from llm_second_opinion.adapters import ADAPTERS, AgentAdapter
from llm_second_opinion.adapters.base import AgentInfraError, Layer
from llm_second_opinion.config import ADVISOR_MODEL, Arm, ConfigError, Experiment, Price
from llm_second_opinion.contracts import AgentResult, ExitReason, RoleUsage
from llm_second_opinion.grading import Grade, grade
from llm_second_opinion.ledger import ItemKey, Ledger
from llm_second_opinion.metering import (
    ItemMeter,
    MeteringError,
    MeteringProxy,
    default_proxy_host,
    preflight,
    read_usage,
    rewrite,
    spend,
    summarize_usage,
)
from llm_second_opinion.metrics import extract, numeric
from llm_second_opinion.picker import BASE_DIR, LOCAL_CHECK, local_check
from llm_second_opinion.report import paired_comparisons, paired_metrics, summarize
from llm_second_opinion.runtime import Container, Runtime
from llm_second_opinion.source import git_state
from llm_second_opinion.tasks import Manifest, Task
from llm_second_opinion.tracing import Span, clip, s_to_ns


class Tracker(Protocol):
    """Where results go besides the ledger; `tracking.Tracker` logs to MLflow."""

    def log_item(
        self,
        key: ItemKey,
        arm_params: dict[str, Any],
        params: dict[str, Any],
        metrics: dict[str, float],
        artifacts: Path,
        trace: Span | None = None,
    ) -> str:
        """Record one finished work item (and its trace) and return its run id."""
        ...

    def log_arm_summary(
        self, arm: str, config_hash: str, arm_params: dict[str, Any], metrics: dict[str, float]
    ) -> None: ...

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


class PhaseError(RuntimeError):
    """Grading or tracking failed after the agent's result was saved: retried on its own."""


class AdvisorRefused(AgentInfraError):
    """The advisor endpoint refused a call (HTTP 401/403: key or quota). Not retried, and the
    batch starts no more items: every further consult would fail the same way."""


ADVISOR_REFUSED = (401, 403)


def advisor_failures(usage: Path) -> list[int]:
    """The HTTP status of each advisor call that got no 2xx answer (0: no answer)."""
    return [
        r.status for r in read_usage(usage) if r.role == "advisor" and not 200 <= r.status < 300
    ]


class Runner:
    """Runs an experiment's work items; `Runner(exp, Path("runs")).run()` is the whole batch.

    Items run on `parallel` worker threads (containers do the work; threads only wait on
    them). Each item is retried after an infrastructure error, and the ledger in
    `runs_dir/<experiment>/` makes a rerun skip what is already done. Tasks in the manifest's
    `test` split run only with `final=True` (see `check_final`).
    """

    def __init__(
        self,
        exp: Experiment,
        runs_dir: Path,
        *,
        runtime: Runtime | None = None,
        tracker: Tracker | None = None,
        parallel: int | None = None,
        final: bool = False,
        echo: Callable[[str], None] = print,
        preflight: bool = True,
        proxy_host: str | None = None,
    ):
        self.exp = exp
        self.manifest = exp.select(Manifest.from_yaml(exp.tasks))
        self.dir = runs_dir / exp.name
        self.ledger = Ledger(self.dir / "ledger.sqlite")
        self._runtime = runtime
        self.tracker = tracker
        self.parallel = parallel or exp.execution.parallel
        self.final = final
        self.echo = echo
        self.adapters = {arm.name: _adapter_for(exp, arm) for arm in exp.arms}
        self.preflight = preflight
        self.proxy_host = proxy_host or default_proxy_host()
        self.halted: str | None = None  # why the batch starts no more items
        self.proxy: MeteringProxy | None = None  # while a batch with model calls runs
        self._hashes: dict[str, str] | None = None

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            self._runtime = Runtime()
        return self._runtime

    @property
    def hashes(self) -> dict[str, str]:
        """Each arm's config hash, with its adapter's fingerprint (for pi this builds the
        bundle image, from Docker's cache when unchanged)."""
        if self._hashes is None:
            self._hashes = arm_hashes(self.exp, self.adapters)
        return self._hashes

    def items(self, arm: str | None = None, task: str | None = None) -> list[WorkItem]:
        """Seed-major order, so a partial batch covers every arm and task evenly."""
        arms = [self.exp.arm(arm)] if arm else self.exp.arms
        tasks = [t for t in self.manifest.tasks if task in (None, t.id)]
        if not tasks:
            raise ConfigError(f"no task {task!r} in {self.exp.tasks}")
        return [
            WorkItem(
                a,
                t,
                seed,
                ItemKey(self.exp.name, a.name, t.id, seed, self.hashes[a.name], task_image(t)),
            )
            for seed in range(self.exp.seeds)
            for t in tasks
            for a in arms
        ]

    def check_final(self, items: list[WorkItem]) -> int:
        """Refuse test-split tasks unless this is the final, confirmatory run; returns how
        many of the items' tasks are in the held-out `test` split."""
        test_tasks = len({i.task.id for i in items if i.task.split == "test"})
        if not test_tasks or self.final:
            return test_tasks
        why = (
            f"split is {self.exp.split!r}"
            if self.exp.split
            else "the experiment sets no split, so it selects test tasks too"
        )
        raise ConfigError(
            f"{test_tasks} task(s) to run are in the manifest's held-out `test` split ({why}). "
            "Test results are the reported, confirmatory numbers, so prompts and settings "
            "must not be tuned on them: develop on `split: dev`, choose the policies to "
            "confirm, then run them on `test` once with --final (recorded in the ledger and "
            "MLflow)."
        )

    def run(self, arm: str | None = None, task: str | None = None) -> list[Outcome]:
        items = self.items(arm, task)
        test_tasks = self.check_final(items)
        self._record_interrupted()
        todo = [i for i in items if self.ledger.status(i.key) != "done"]
        skipped = [Outcome(i.key, "skipped") for i in items if i not in todo]
        if skipped:
            self.echo(f"skipping {len(skipped)} item(s) already done")
        arms = list({i.arm.name: i.arm for i in todo}.values())
        env = _secrets(self.exp, arms)
        started = self.ledger.record_session(self.exp.name, self.exp.split, test_tasks, self.final)
        self.ledger.record_fingerprints(
            self.exp.name,
            {
                a.name: _call(self.adapters[a.name], "fingerprint", {})
                for a in {i.arm.name: i.arm for i in items}.values()
            },
        )
        with self._metering(arms, env, started):
            self.echo(f"running {len(todo)} item(s), {self.parallel} at a time")
            pool = ThreadPoolExecutor(self.parallel)
            try:
                outcomes = list(pool.map(lambda i: self._run_item(i, env), todo))
                if self.halted:
                    self.echo(f"stopped early, {self.halted}; rerun to resume")
            finally:
                # On Ctrl-C, items in flight finish and are recorded; queued ones are dropped
                # and run on the next resume.
                pool.shutdown(cancel_futures=True)
                if self.tracker:
                    self._log_summaries()
                    self.tracker.close()
        return skipped + outcomes

    def _record_interrupted(self) -> None:
        """Attempts a dead process left running are `interrupted`; what they spent is read
        from their usage.jsonl."""
        for row in self.ledger.interrupted(self.exp.name):
            key = ItemKey(*(row[c] for c in ItemKey.__dataclass_fields__))
            usage = self.dir / row["dir"] / "usage.jsonl"
            if usage.exists():
                fields = spend(usage, self._prices(_arm_or_none(self.exp, key.arm)))
                self.ledger.update_attempt(key, row["attempt"], **fields)

    @contextmanager
    def _metering(self, arms: list[Arm], env: dict[str, str], started: float) -> Iterator[None]:
        """The batch's metering proxy, if any arm's agent calls models, after a usage
        preflight of every endpoint those arms use. The proxy holds the secrets."""
        keys = {
            key
            for arm in arms
            if self.adapters[arm.name].uses_models
            for key in [arm.executor, *([ADVISOR_MODEL] if arm.advisor else [])]
        }
        if not keys:
            yield
            return
        self.proxy = MeteringProxy(self.proxy_host, env=env).start()
        try:
            self.echo(f"metering model calls through {self.proxy_host}:{self.proxy.port}")
            if self.preflight:
                endpoints = {k: self.exp.models[k] for k in sorted(keys)}
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started))
                out = self.dir / "preflight" / f"{stamp}.{int(started * 1e6) % 1_000_000:06d}"
                try:
                    preflight(self.proxy, endpoints, out)
                finally:
                    self._record_preflight(endpoints, out, started)
                self.echo(f"usage preflight passed: {', '.join(endpoints)}")
            else:
                self.echo("usage preflight skipped (--no-preflight: mock server and tests only)")
            yield
        finally:
            self.proxy.stop()
            self.proxy = None

    def _record_preflight(self, endpoints: dict, out: Path, started: float) -> None:
        """The preflight calls' spend, in the ledger: part of the experiment's total."""
        for key, endpoint in endpoints.items():
            records = read_usage(out / key / "usage.jsonl")
            fields = spend(out / key / "usage.jsonl", {"executor": self.exp.prices.get(key)})
            self.ledger.record_preflight(
                self.exp.name,
                started,
                model_key=key,
                model=endpoint.model,
                calls=fields["model_calls"],
                failed_calls=fields["failed_calls"],
                prompt_tokens=sum(r.prompt_tokens for r in records),
                completion_tokens=sum(r.completion_tokens for r in records),
                cached_tokens=sum(r.cached_tokens for r in records),
                reasoning_tokens=sum(r.reasoning_tokens for r in records),
                cost_usd=fields["cost_usd"],
            )

    def _log_summaries(self) -> None:
        """Per-arm results so far (all resumes of this config), on each arm's MLflow run."""
        rows = self.ledger.rows(self.exp.name)
        attempts = self.ledger.attempts(self.exp.name)
        images = {t.id: task_image(t) for t in self.manifest.tasks}
        pairs = paired_comparisons(self.exp, rows, hashes=self.hashes, images=images)
        paired = {p.arm: paired_metrics(p) for p in pairs}
        summaries = summarize(
            self.exp, rows, len(self.manifest.tasks), self.hashes, images, attempts
        )
        for s in summaries:
            arm = self.exp.arm(s.arm)
            metrics = {"items_planned": s.planned, "items_done": s.done, "items_failed": s.failed}
            if s.done:
                low, high = s.interval or (0.0, 0.0)
                metrics |= {
                    "resolved": s.resolved,
                    "resolve_rate": s.rate or 0.0,
                    "resolve_rate_ci_low": low,
                    "resolve_rate_ci_high": high,
                    "median_turns": median(s.turns),
                    "median_duration_s": median(s.durations),
                } | {f"exit_{reason}": n for reason, n in s.exit_reasons.items()}
                metrics |= paired.get(s.arm, {})
            if s.tokens:
                metrics |= {
                    "tokens_sum": sum(s.tokens),
                    "tokens_median": median(s.tokens),
                    "advisor_tokens_sum": sum(s.advisor_tokens),
                    "advisor_tokens_median": median(s.advisor_tokens),
                }
            if s.cost is not None:
                metrics |= {"cost_usd_sum": s.cost, "cost_usd_median": median(s.costs)}
            if s.cost_per_resolved is not None:
                metrics["cost_usd_per_resolved"] = s.cost_per_resolved
            metrics["attempts"] = s.attempts
            metrics |= {f"attempts_{status}": n for status, n in s.attempt_statuses.items()}
            if s.spent is not None:
                metrics["spent_usd_all_attempts"] = s.spent
            params = self._pinned_inputs(arm, self.adapters[arm.name])
            self.tracker.log_arm_summary(arm.name, s.config_hash, params, metrics)

    # --- one item ----------------------------------------------------------------------

    def _run_item(self, item: WorkItem, env: dict[str, str]) -> Outcome:
        """Attempts until one is done or the retries are used up. An attempt whose agent
        finished but whose grading or tracking failed (in this batch or an earlier one) is
        finished from its result on disk, without running the agent again."""
        retries = self.exp.execution.retries
        failures = 0
        resumed = self.ledger.resumable(item.key)
        attempt = (resumed["attempt"], Path(resumed["dir"])) if resumed else None
        while True:
            if self.halted:
                self.echo(f"{item.key}: not started ({self.halted})")
                return Outcome(item.key, "skipped")
            if attempt is None:
                self.ledger.start(item.key)
                attempt = self.ledger.new_attempt(item.key, self.dir)
            else:
                self.ledger.finish(item.key, "running")
                self.ledger.update_attempt(item.key, attempt[0], status="running", ended=None)
            k, rel = attempt
            try:
                result, graded, fields = self._attempt(item, env, k, rel)
            # One item's infrastructure failure (Docker, git, I/O, the model endpoint) must
            # not stop the batch; it is retried, then recorded in the ledger.
            except Exception as e:  # noqa: BLE001
                failures += 1
                error = f"{type(e).__name__}: {e}"
                self.ledger.update_attempt(
                    item.key, k, status="failed", error=error, ended=time.time()
                )
                if not isinstance(e, PhaseError):
                    attempt = None  # the agent runs again, in a new attempt directory
                if isinstance(e, AdvisorRefused):
                    self.halted = f"advisor refused: {e}"
                if failures <= retries and not isinstance(e, AdvisorRefused):
                    self.echo(f"{item.key}: attempt {k} failed ({e}); retrying")
                    continue
                self.ledger.finish(item.key, "failed", error=error, attempt_dir=str(rel))
                self.echo(f"{item.key}: FAILED after {failures} try(s): {e}")
                return Outcome(item.key, "failed")
            self.ledger.update_attempt(
                item.key, k, status="done", ended=time.time(), exit_reason=result.exit_reason.value
            )
            self.ledger.finish(item.key, "done", **fields)
            self.echo(f"{item.key}: {result.exit_reason.value}, {graded.reason}")
            return Outcome(item.key, "done", graded.resolved)

    def _attempt(
        self, item: WorkItem, env: dict[str, str], k: int, rel: Path
    ) -> tuple[AgentResult, Grade, dict[str, Any]]:
        """Steps 3-7 of the run lifecycle for one attempt, in its own directory. The agent
        phase is skipped when its result is already there; grading too, when grade.json is.
        Returns the result, the grade, and the item's ledger columns."""
        out = self.dir / rel
        adapter = self.adapters[item.arm.name]
        if not (out / "result.json").exists():
            out.mkdir(parents=True, exist_ok=True)
            self._agent_phase(item, adapter, env, k, out)
        result = AgentResult.model_validate_json((out / "result.json").read_text())
        try:
            graded = self._grade_phase(item, out)
            trajectory = self._metrics(adapter, out)
            run_id = self._track(item, adapter, out, k, result, graded, trajectory)
        except Exception as e:
            raise PhaseError(f"after the agent finished: {type(e).__name__}: {e}") from e
        fields = self._item_fields(item, result, graded, out, rel)
        return result, graded, fields | {"mlflow_run_id": run_id}

    def _agent_phase(
        self, item: WorkItem, adapter: AgentAdapter, env: dict[str, str], k: int, out: Path
    ) -> None:
        """Run the agent and save its result (result.json last: its presence means the
        agent phase is complete). What the attempt spent is recorded however it ends."""
        layer = adapter.build_layer(task_image(item.task))
        self._write_item_json(out, self._item_record(item, adapter, k))
        meter = None
        if adapter.uses_models:
            endpoints = {"executor": self.exp.models[item.arm.executor]}
            if item.arm.advisor:
                endpoints["advisor"] = self.exp.models[ADVISOR_MODEL]
            meter = self.proxy.register(
                endpoints, out / "usage.jsonl", self.exp.limits.max_tokens, attempt=k
            )
        started = time.time()
        try:
            result = self._run_agent(item, adapter, layer, out, env, meter)
        except AgentInfraError as e:
            if e.result is not None:  # kept for reading; the attempt still failed
                (out / "agent-result.json").write_text(e.result.model_dump_json(indent=2))
            raise
        finally:
            if meter:
                # Wait for any call still in flight, so the attempt's record is complete.
                with suppress(MeteringError):
                    meter.close()
                self.proxy.unregister(meter)
                fields = spend(meter.usage_path, self._prices(item.arm))
                self.ledger.update_attempt(item.key, k, **fields)
            self._write_item_json(out, {"agent_started": started, "agent_ended": time.time()})
        # A run with a failed consult is not the arm's treatment (the agent goes on without
        # the advice): the attempt fails and is retried, unless the advisor refused outright.
        failed = advisor_failures(meter.usage_path) if meter and item.arm.advisor else []
        if failed:
            (out / "agent-result.json").write_text(result.model_dump_json(indent=2))
            message = f"{len(failed)} advisor call(s) failed: HTTP {sorted(set(failed))}"
            if set(failed) & set(ADVISOR_REFUSED):
                raise AdvisorRefused(message, result)
            raise AgentInfraError(message, result)
        (out / "patch.diff").write_text(result.diff)
        (out / "result.json").write_text(result.model_dump_json(indent=2))
        self.ledger.update_attempt(
            item.key, k, agent_ended=time.time(), exit_reason=result.exit_reason.value
        )

    def _run_agent(
        self,
        item: WorkItem,
        adapter: AgentAdapter,
        layer: Layer,
        out: Path,
        env: dict[str, str],
        meter: ItemMeter | None,
    ) -> AgentResult:
        """Run the agent in its container and return its result. The agent's artifacts and
        events are copied out however the run ends.

        With a meter, the agent reaches its models only through the proxy: the run config
        names the item's proxy routes, and the agent gets no API keys.
        """
        config = self.exp.run_config(item.arm, item.task.id, item.seed, self.hashes[item.arm.name])
        if meter:
            config, env = rewrite(config, self.proxy, meter), {}
        rendered = config.model_dump_json(indent=2)
        (out / "advisor.json").write_text(rendered)
        with self._container(layer.image, f"agent {item.key}", layer.volumes) as box:
            box.write("/run/advisor.json", rendered)
            try:
                result = adapter.run(box, item.task, config, self.exp.limits, env)
            finally:
                self._copy_artifacts(box, adapter, config.events_path, out)
        if meter:
            meter.close()  # raises if a call had no usage: the item must not count as done
            usage = summarize_usage(read_usage(meter.usage_path))
            update: dict[str, Any] = {"usage": usage}
            if meter.refused:
                # The agent stopped because the proxy refused calls over the budget.
                update |= {"exit_reason": ExitReason.TOKEN_LIMIT, "detail": None}
            result = result.model_copy(update=update)
        return result

    def _copy_artifacts(
        self, box: Container, adapter: AgentAdapter, events: str, out: Path
    ) -> None:
        """events.jsonl and the adapter's artifacts, each on its own: one failing copy (a
        container gone) must not lose the others, nor hide the agent's own error."""
        for path, name in [(events, "events.jsonl")] + [
            (p, Path(p).name) for p in adapter.artifacts
        ]:
            try:
                content = box.read(path)
            except Exception as e:  # noqa: BLE001
                content = None
                (out / f"{name}.copy-error").write_text(f"{type(e).__name__}: {e}\n")
            if content is not None:
                (out / name).write_text(content)
        if not (out / "events.jsonl").exists():
            (out / "events.jsonl").write_text("")

    def _grade_phase(self, item: WorkItem, out: Path) -> Grade:
        """Grade the saved patch in a fresh container, unless grade.json says it was."""
        if (out / "grade.json").exists():
            log = (out / "grade.log").read_text() if (out / "grade.log").exists() else ""
            return Grade.from_record(json.loads((out / "grade.json").read_text()), log)
        return self._grade_into(item.task, task_image(item.task), out, f"grade {item.key}")

    def _grade_into(self, task: Task, image: str, out: Path, name: str) -> Grade:
        """Grade out/patch.diff and write grade.json, grade.log (and build.log)."""
        patch = (out / "patch.diff").read_text()
        started = time.time()
        timeout = self.exp.execution.grade_minutes * 60
        with self._container(image, name) as box:
            graded = grade(box, task, patch, timeout)
        (out / "grade.log").write_text(graded.log)
        if graded.build_log is not None:
            (out / "build.log").write_text(graded.build_log)
        (out / "grade.json").write_text(json.dumps(graded.record(), indent=2))
        grading = {"commit": git_state()["commit"], "host": socket.gethostname()}
        self._write_item_json(
            out, {"grade_started": started, "grade_ended": time.time(), "grading": grading}
        )
        return graded

    def regrade(self, arm: str | None = None, task: str | None = None) -> list[Outcome]:
        """Grade the stored patch of every done item again with the current grader, without
        running any agent; the ledger's grade columns follow. Covers every config hash in
        the ledger, and item directories of the layout before attempt directories. The
        previous grade files are kept as grade.v<grader version>.json (and .log)."""
        tasks = {t.id: t for t in self.manifest.tasks}
        if task is not None and task not in tasks:
            raise ConfigError(f"no task {task!r} in {self.exp.tasks}")
        rows = [
            r
            for r in self.ledger.rows(self.exp.name)
            if r["status"] == "done"
            and r["task"] in tasks
            and arm in (None, r["arm"])
            and task in (None, r["task"])
        ]
        self.echo(f"regrading {len(rows)} done item(s), {self.parallel} at a time")
        with ThreadPoolExecutor(self.parallel) as pool:
            return list(pool.map(lambda r: self._regrade_row(r, tasks[r["task"]]), rows))

    def done_rows(self, arm: str) -> list[dict[str, Any]]:
        """Done ledger rows of `arm` under its current config hash, for tasks in the manifest."""
        tasks = {t.id for t in self.manifest.tasks}
        return [
            r
            for r in self.ledger.rows(self.exp.name)
            if r["status"] == "done"
            and r["arm"] == arm
            and r["config_hash"] == self.hashes[arm]
            and r["task"] in tasks
            and r["attempt_dir"]
        ]

    def local_checks(self, arm: str) -> tuple[int, int]:
        """The picker's local check (`picker.local_check`) of every done item of `arm`, and of
        each of its tasks' base commit; kept on disk, so a rerun does only what is missing.
        Returns (checked, failed)."""
        tasks = {t.id: t for t in self.manifest.tasks}
        rows = self.done_rows(arm)
        jobs: list[tuple[Task, str, str, Path]] = []
        for t in sorted({r["task"] for r in rows}):
            path = self.dir / BASE_DIR / f"{t}.json"
            if not path.exists():
                jobs.append((tasks[t], task_image(tasks[t]), "", path))
        for r in rows:
            out = self.dir / r["attempt_dir"]
            if not (out / LOCAL_CHECK).exists():
                patch = (out / "patch.diff").read_text() if (out / "patch.diff").exists() else ""
                image = r["image_id"] or task_image(tasks[r["task"]])
                jobs.append((tasks[r["task"]], image, patch, out / LOCAL_CHECK))
        self.echo(f"local checks: {len(jobs)} to run, {self.parallel} at a time")
        timeout = self.exp.execution.grade_minutes * 60

        def one(job: tuple[Task, str, str, Path]) -> bool:
            task, image, patch, path = job
            try:
                with self._container(image, f"local-check {task.id}") as box:
                    record = local_check(box, task, patch, timeout)
            except Exception as e:  # noqa: BLE001 - one item must not stop the others
                self.echo(f"{path}: local check failed: {type(e).__name__}: {e}")
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2))
            return True

        with ThreadPoolExecutor(self.parallel) as pool:
            ok = list(pool.map(one, jobs))
        return sum(ok), len(ok) - sum(ok)

    def _regrade_row(self, row: dict[str, Any], task: Task) -> Outcome:
        key = ItemKey(*(row[c] for c in ItemKey.__dataclass_fields__))
        rel = row["attempt_dir"] or str(Path(key.arm) / key.task / f"seed-{key.seed}")
        out = self.dir / rel
        if not (out / "patch.diff").exists():
            self.echo(f"{key}: no patch.diff in {out}; skipped")
            return Outcome(key, "skipped")
        old = json.loads((out / "grade.json").read_text()) if (out / "grade.json").exists() else {}
        version = old.get("grader_version", 1)
        for name in ("grade.json", "grade.log", "build.log"):
            path = out / name
            if path.exists():
                path.rename(out / f"{path.stem}.v{version}{path.suffix}")
        try:
            graded = self._grade_into(task, key.image_id or task_image(task), out, f"regrade {key}")
        except Exception as e:  # noqa: BLE001 - one item must not stop the others
            self.echo(f"{key}: regrading failed: {type(e).__name__}: {e}")
            return Outcome(key, "failed")
        fields = {
            "resolved": graded.resolved,
            "grade": graded.reason,
            "build_failed": graded.build_failed,
            "edited_tests": len(graded.agent_touched_test_files),
            **_grade_counts(graded),
        }
        self.ledger.finish(key, "done", **fields)
        before = old.get("reason", row["grade"])
        self.echo(f"{key}: {before} -> {graded.reason}")
        return Outcome(key, "done", graded.resolved)

    def _metrics(self, adapter: AgentAdapter, out: Path) -> dict[str, Any]:
        """metrics.json; a parsing problem is recorded there rather than failing the item."""
        try:
            metrics = extract(out, _call(adapter, "metrics", {}, out))
        except Exception as e:  # noqa: BLE001
            metrics = {"error": f"{type(e).__name__}: {e}"}
        (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
        return metrics

    def _track(
        self,
        item: WorkItem,
        adapter: AgentAdapter,
        out: Path,
        k: int,
        result: AgentResult,
        graded: Grade,
        trajectory: dict[str, Any],
    ) -> str | None:
        if not self.tracker:
            return None
        usage = self._spend_of(item, out)
        times = json.loads((out / "item.json").read_text())
        return self.tracker.log_item(
            item.key,
            arm_params=self._pinned_inputs(item.arm, adapter),
            params={
                "task": item.task.id,
                "seed": item.seed,
                "attempt": k,
                "image_id": item.key.image_id,
                "grade": graded.reason,
                "exit_reason": result.exit_reason.value,
            },
            metrics=numeric(trajectory)
            | {
                "resolved": float(graded.resolved),
                "turns": result.turns,
                "duration_s": result.duration_s,
                "build_failed": float(graded.build_failed),
            }
            | _grade_counts(graded)
            | {k: v for k, v in usage.items() if v is not None},
            artifacts=out,
            trace=self._trace(item, adapter, out, result, graded, times),
        )

    def _item_fields(
        self, item: WorkItem, result: AgentResult, graded: Grade, out: Path, rel: Path
    ) -> dict[str, Any]:
        """The ledger's columns for a done item, from its counted attempt."""
        advisor = item.arm.advisor
        return {
            "resolved": graded.resolved,
            "grade": graded.reason,
            "exit_reason": result.exit_reason.value,
            "turns": result.turns,
            "duration_s": result.duration_s,
            "error": None,
            "prompt_hash": self.exp.prompt_set(advisor.prompts).hash if advisor else None,
            "level": advisor.level if advisor else None,
            "interventions": ",".join(advisor.interventions) if advisor else None,
            "build_failed": graded.build_failed,
            "edited_tests": len(graded.agent_touched_test_files),
            "attempt_dir": str(rel),
            **_grade_counts(graded),
            **self._spend_of(item, out),
        }

    def _spend_of(self, item: WorkItem, out: Path) -> dict[str, Any]:
        """The attempt's spend columns; all null when the agent is not metered."""
        if not self.adapters[item.arm.name].uses_models:
            return {}
        return spend(out / "usage.jsonl", self._prices(item.arm))

    def _item_record(self, item: WorkItem, adapter: AgentAdapter, k: int) -> dict[str, Any]:
        """item.json at the start of an attempt: what ran, where, and from which checkout."""
        advisor = item.arm.advisor
        return {
            "experiment": self.exp.name,
            "arm": item.arm.name,
            "task": item.task.id,
            "seed": item.seed,
            "attempt": k,
            "config_hash": item.key.config_hash,
            "manifest_version": self.manifest.version,
            "task_image": item.task.image,
            "task_image_id": item.task.image_id,
            "base_commit": item.task.base_commit,
            "prompt_hash": self.exp.prompt_set(advisor.prompts).hash if advisor else None,
            "agent": {"name": adapter.name, "version": adapter.version}
            | _call(adapter, "provenance", {}),
            "harness": {"version": __version__, **git_state()},
            "host": socket.gethostname(),
            "parallel": self.parallel,
        }

    @staticmethod
    def _write_item_json(out: Path, fields: dict[str, Any]) -> None:
        path = out / "item.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps(record | fields, indent=2))

    def _prices(self, arm: Arm | None) -> dict[str, Price | None]:
        """The price of the model behind each role of the arm (no prices for an arm no
        longer in the experiment)."""
        if arm is None:
            return {}
        prices = {"executor": self.exp.prices.get(arm.executor)}
        if arm.advisor:
            prices["advisor"] = self.exp.prices.get(ADVISOR_MODEL)
        return prices

    def _trace(
        self,
        item: WorkItem,
        adapter: AgentAdapter,
        out: Path,
        result: AgentResult,
        graded: Grade,
        times: dict[str, Any],
    ) -> Span:
        """The item's trace: the agent's own spans under an agent span, then grading."""
        agent_start = s_to_ns(times.get("agent_started") or 0)
        agent_end = s_to_ns(times.get("agent_ended") or 0)
        grade_start = s_to_ns(times.get("grade_started") or times.get("agent_ended") or 0)
        grade_end = s_to_ns(times.get("grade_ended") or 0)
        crashed = result.exit_reason.value == "crash"
        children = adapter.spans(out)
        agent = Span(
            f"{adapter.name} {adapter.version}",
            "AGENT",
            agent_start,
            agent_end,
            inputs={"problem_statement": clip(item.task.problem_statement)},
            outputs={"exit_reason": result.exit_reason.value, "diff": clip(result.diff)},
            attributes={"turns": result.turns} | _token_attributes(result.usage, children),
            error=result.detail if crashed else None,
            children=children,
        )
        tests = Counter(graded.tests.values())
        grading = Span(
            "grade",
            "EVALUATOR",
            grade_start,
            grade_end,
            inputs={"patch_bytes": len(result.diff)},
            outputs={
                "resolved": graded.resolved,
                "reason": graded.reason,
                "tests": dict(tests),
                "build_failed": graded.build_failed,
            }
            | _grade_counts(graded),
        )
        return Span(
            str(item.key),
            "CHAIN",
            agent_start,
            grade_end,
            inputs={"task": item.task.id, "seed": item.seed},
            outputs={
                "resolved": graded.resolved,
                "grade": graded.reason,
                "exit_reason": result.exit_reason.value,
            },
            attributes={"arm": item.arm.name, "config_hash": item.key.config_hash},
            children=[agent, grading],
        )

    @contextmanager
    def _container(
        self, image: str, name: str, volumes: dict[str, str] | None = None
    ) -> Iterator[Container]:
        """A capped container that is removed however the block exits."""
        exe = self.exp.execution
        box = self.runtime.start(image, exe.cpus, exe.memory_gb, name, volumes)
        try:
            yield box
        finally:
            box.remove()

    def _pinned_inputs(self, arm: Arm, adapter: AgentAdapter) -> dict[str, Any]:
        """Everything a reader needs to reproduce the arm, logged once per arm run."""
        executor = self.exp.models[arm.executor]
        advisor = self.exp.models[ADVISOR_MODEL] if arm.advisor else None
        fingerprint = _call(adapter, "fingerprint", {})
        return {
            "config_hash": self.hashes[arm.name],
            "manifest_version": self.manifest.version,
            "split": self.exp.split,
            "tasks": len(self.manifest.tasks),
            "harness_version": __version__,
            "agent": adapter.name,
            "agent_version": adapter.version,
            "agent_options": json.dumps(self.exp.agent_spec(arm).options, sort_keys=True),
            **{f"agent_{name}": value for name, value in fingerprint.items()},
            "executor_model": executor.model,
            "executor_reasoning_effort": executor.reasoning_effort,
            "executor_temperature": executor.temperature,
            "executor_top_p": executor.top_p,
            "executor_sampling_seed": executor.sampling_seed,
            "advisor_model": advisor.model if advisor else None,
            "advisor_reasoning_effort": advisor.reasoning_effort if advisor else None,
            "advisor_level": arm.advisor.level if arm.advisor else None,
            "advisor_interventions": (",".join(arm.advisor.interventions) if arm.advisor else None),
            "advisor_max_consults": arm.advisor.max_consults if arm.advisor else None,
            "prompt_set": arm.advisor.prompts if arm.advisor else None,
            "prompt_hash": self.exp.prompt_set(arm.advisor.prompts).hash if arm.advisor else None,
            "max_turns": self.exp.limits.max_turns,
            "wall_minutes": self.exp.limits.wall_minutes,
            "max_tokens": self.exp.limits.max_tokens,
        }


def task_image(task: Task) -> str:
    """The image an item runs on: a validated manifest pins it by ID, so a rebuilt image is
    not used by mistake."""
    return task.image_id or task.image


def arm_hashes(exp: Experiment, adapters: dict[str, AgentAdapter] | None = None) -> dict[str, str]:
    """Each arm's config hash with its adapter's fingerprint; adapters are built when not
    given. For pi this needs Docker (the bundle image's ID)."""
    adapters = adapters or {arm.name: _adapter_for(exp, arm) for arm in exp.arms}
    return {
        arm.name: exp.config_hash(arm, _call(adapters[arm.name], "fingerprint", {}))
        for arm in exp.arms
    }


def _call(adapter: AgentAdapter, method: str, default: Any, *args: Any) -> Any:
    """An optional adapter method (test agents may not have it)."""
    fn = getattr(adapter, method, None)
    return fn(*args) if fn else default


def _arm_or_none(exp: Experiment, name: str) -> Arm | None:
    try:
        return exp.arm(name)
    except KeyError:
        return None


def _grade_counts(graded: Grade) -> dict[str, int]:
    counts = {}
    for name, tally in (("f2p", graded.f2p), ("p2p", graded.p2p)):
        if tally is not None:
            counts |= {f"{name}_passed": tally["passed"], f"{name}_total": tally["total"]}
    return counts


def _adapter_for(exp: Experiment, arm: Arm) -> AgentAdapter:
    spec = exp.agent_spec(arm)
    if spec.adapter not in ADAPTERS:
        known = ", ".join([*exp.agents, *ADAPTERS])
        raise ConfigError(f"arm {arm.name}: unknown agent {arm.agent!r}; known: {known}")
    try:
        adapter = ADAPTERS[spec.adapter](spec)
    except ConfigError as e:
        raise ConfigError(f"arm {arm.name}: {e}") from e
    if arm.advisor and "advisor" not in adapter.capabilities:
        raise ConfigError(f"arm {arm.name}: agent {arm.agent!r} does not support an advisor")
    return adapter


def _token_attributes(usage: list[RoleUsage], spans: list[Span]) -> dict[str, int]:
    """The proxy's per-role counts (the source of truth) next to the totals the agent's own
    model spans report, so the two can be compared in the trace."""
    attributes = {}
    for u in usage:
        attributes[f"proxy.{u.role}.calls"] = u.calls
        attributes[f"proxy.{u.role}.prompt_tokens"] = u.prompt_tokens
        attributes[f"proxy.{u.role}.completion_tokens"] = u.completion_tokens
        attributes[f"proxy.{u.role}.cached_tokens"] = u.cached_tokens
        attributes[f"proxy.{u.role}.reasoning_tokens"] = u.reasoning_tokens
    models = [s for s in _walk(spans) if s.kind == "CHAT_MODEL"]
    if models:
        attributes["agent.model_calls"] = len(models)
        for name in ("input", "output", "cache_read"):
            attributes[f"agent.tokens.{name}"] = sum(
                int(s.attributes.get(f"tokens.{name}") or 0) for s in models
            )
    return attributes


def _walk(spans: list[Span]) -> Iterator[Span]:
    for span in spans:
        yield span
        yield from _walk(span.children)


def _secrets(exp: Experiment, arms: Iterable[Arm]) -> dict[str, str]:
    """API keys and secret headers the arms' models name, read from the host environment.
    Fails before any run."""
    names = set()
    for arm in arms:
        models = [exp.models[arm.executor]] + ([exp.models[ADVISOR_MODEL]] if arm.advisor else [])
        names |= {m.api_key_env for m in models if m.api_key_env}
        names |= {var for m in models for var in m.header_env.values()}
    missing = sorted(n for n in names if n not in os.environ)
    if missing:
        raise ConfigError(f"unset secret variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
