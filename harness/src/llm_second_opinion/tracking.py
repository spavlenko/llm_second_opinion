"""MLflow logging: one experiment per study, a parent run per arm, a child run per work item.

Each item's run also gets a trace (item -> agent -> turns -> model and tool calls, and
grading), and each arm's run gets summary metrics at the end of a batch.
"""

from __future__ import annotations

import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import mlflow
from mlflow import MlflowClient
from mlflow.entities import Metric, Param

from llm_second_opinion.ledger import ItemKey
from llm_second_opinion.tracing import Span

_ARM = "lso.arm"
_HASH = "lso.config_hash"
_FINAL = "lso.final"  # set on runs from `bench run --final` (test-split tasks)
DEFAULT_URI = "http://127.0.0.1:5050"  # scripts/mlflow-server.sh
REPO = Path(__file__).resolve().parents[3]


class TrackingError(RuntimeError):
    pass


def check_server(uri: str, timeout_s: float = 5) -> None:
    """Fail fast when an HTTP tracking server is down (the client would retry for minutes)."""
    if not uri.startswith(("http://", "https://")):
        return
    try:
        with urllib.request.urlopen(f"{uri.rstrip('/')}/health", timeout=timeout_s) as resp:
            if resp.status == 200:
                return
    except (urllib.error.URLError, OSError) as e:
        raise TrackingError(
            f"MLflow is not reachable at {uri} ({e}); start it with scripts/mlflow-server.sh"
        ) from e
    raise TrackingError(f"MLflow at {uri} is not healthy")


class Tracker:
    def __init__(self, uri: str, experiment: str, final: bool = False):
        # Trace export goes to the global tracking URI, whatever the client's; one tracker
        # per process, so setting it here is safe.
        mlflow.set_tracking_uri(uri)
        self.client = MlflowClient(uri)
        found = self.client.get_experiment_by_name(experiment)
        self.experiment_id = (
            found.experiment_id if found else self.client.create_experiment(experiment)
        )
        self._arm_runs: dict[str, str] = {}
        self._lock = threading.Lock()
        self._git = _git_tags()
        self._final = {_FINAL: "true"} if final else {}

    def log_item(
        self,
        key: ItemKey,
        arm_params: dict[str, Any],
        params: dict[str, Any],
        metrics: dict[str, float],
        artifacts: Path,
        trace: Span | None = None,
    ) -> str:
        parent = self._arm_run(key.arm, key.config_hash, arm_params)
        run = self.client.create_run(
            self.experiment_id,
            run_name=f"{key.task}/seed-{key.seed}",
            tags={
                "mlflow.parentRunId": parent,
                _ARM: key.arm,
                _HASH: key.config_hash,
                **self._final,
            },
        )
        run_id = run.info.run_id
        self.client.log_batch(
            run_id,
            metrics=[Metric(k, float(v), 0, 0) for k, v in metrics.items()],
            params=[Param(k, str(v)) for k, v in params.items()],
        )
        self.client.log_artifacts(run_id, str(artifacts))
        if trace is not None:
            self.log_trace(trace, run_id)
        self.client.set_terminated(run_id)
        return run_id

    def log_trace(self, root: Span, run_id: str) -> str:
        """Write a span tree as one trace linked to `run_id`, with the recorded times."""
        span = self.client.start_trace(
            root.name,
            span_type=root.kind,
            inputs=root.inputs,
            attributes=root.attributes,
            experiment_id=self.experiment_id,
            start_time_ns=root.start_ns,
            run_id=run_id,
        )
        trace_id = span.trace_id
        for child in root.children:
            self._log_span(child, trace_id, span.span_id)
        self.client.end_trace(
            trace_id,
            outputs=root.outputs,
            status="ERROR" if root.error else "OK",
            end_time_ns=max(root.end_ns, root.start_ns),
        )
        # Traces are exported in the background; write this one before the item counts as done.
        mlflow.flush_trace_async_logging()
        return trace_id

    def _log_span(self, node: Span, trace_id: str, parent_id: str) -> None:
        span = self.client.start_span(
            node.name,
            trace_id=trace_id,
            parent_id=parent_id,
            span_type=node.kind,
            inputs=node.inputs,
            attributes=node.attributes,
            start_time_ns=node.start_ns,
        )
        for child in node.children:
            self._log_span(child, trace_id, span.span_id)
        attributes = {"error": node.error} if node.error else None
        self.client.end_span(
            trace_id,
            span.span_id,
            outputs=node.outputs,
            attributes=attributes,
            status="ERROR" if node.error else "OK",
            end_time_ns=max(node.end_ns, node.start_ns),
        )

    def log_arm_summary(
        self, arm: str, config_hash: str, arm_params: dict[str, Any], metrics: dict[str, float]
    ) -> None:
        """Summary metrics on the arm's run (replacing earlier values at step 0)."""
        run_id = self._arm_run(arm, config_hash, arm_params)
        self.client.log_batch(
            run_id, metrics=[Metric(k, float(v), 0, 0) for k, v in metrics.items()]
        )

    def close(self) -> None:
        for run_id in self._arm_runs.values():
            self.client.set_terminated(run_id)

    def _arm_run(self, arm: str, config_hash: str, params: dict[str, Any]) -> str:
        """The arm's parent run, reused across resumes while its config hash is unchanged."""
        with self._lock:
            if arm in self._arm_runs:
                return self._arm_runs[arm]
            found = self.client.search_runs(
                [self.experiment_id],
                f"tags.`{_ARM}` = '{arm}' AND tags.`{_HASH}` = '{config_hash}' "
                "AND tags.`lso.level` = 'arm'",
                max_results=1,
            )
            if found:
                run_id = found[0].info.run_id
            else:
                tags = {_ARM: arm, _HASH: config_hash, "lso.level": "arm", **self._git}
                run_id = self.client.create_run(
                    self.experiment_id, run_name=arm, tags=tags
                ).info.run_id
                self.client.log_batch(run_id, params=[Param(k, str(v)) for k, v in params.items()])
            for name, value in self._final.items():
                self.client.set_tag(run_id, name, value)
            self._arm_runs[arm] = run_id
            return run_id


def _git_tags() -> dict[str, str]:
    """The harness checkout's commit, and whether it had uncommitted changes."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("rev-parse", "HEAD")
    if not commit:
        return {}
    return {
        "mlflow.source.git.commit": commit,
        "lso.git_dirty": str(bool(git("status", "--porcelain"))),
    }
