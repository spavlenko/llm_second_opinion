"""MLflow logging: one experiment per study, a parent run per arm, a child run per work item."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from mlflow import MlflowClient
from mlflow.entities import Metric, Param

from llm_second_opinion.ledger import ItemKey

_ARM = "lso.arm"
_HASH = "lso.config_hash"


class Tracker:
    def __init__(self, uri: str, experiment: str):
        self.client = MlflowClient(uri)
        found = self.client.get_experiment_by_name(experiment)
        self.experiment_id = (
            found.experiment_id if found else self.client.create_experiment(experiment)
        )
        self._arm_runs: dict[str, str] = {}
        self._lock = threading.Lock()

    def log_item(
        self,
        key: ItemKey,
        arm_params: dict[str, Any],
        params: dict[str, Any],
        metrics: dict[str, float],
        artifacts: Path,
    ) -> str:
        parent = self._arm_run(key, arm_params)
        run = self.client.create_run(
            self.experiment_id,
            run_name=f"{key.task}/seed-{key.seed}",
            tags={"mlflow.parentRunId": parent, _ARM: key.arm, _HASH: key.config_hash},
        )
        run_id = run.info.run_id
        self.client.log_batch(
            run_id,
            metrics=[Metric(k, float(v), 0, 0) for k, v in metrics.items()],
            params=[Param(k, str(v)) for k, v in params.items()],
        )
        self.client.log_artifacts(run_id, str(artifacts))
        self.client.set_terminated(run_id)
        return run_id

    def close(self) -> None:
        for run_id in self._arm_runs.values():
            self.client.set_terminated(run_id)

    def _arm_run(self, key: ItemKey, params: dict[str, Any]) -> str:
        """The arm's parent run, reused across resumes while its config hash is unchanged."""
        with self._lock:
            if key.arm in self._arm_runs:
                return self._arm_runs[key.arm]
            found = self.client.search_runs(
                [self.experiment_id],
                f"tags.`{_ARM}` = '{key.arm}' AND tags.`{_HASH}` = '{key.config_hash}' "
                "AND tags.`lso.level` = 'arm'",
                max_results=1,
            )
            if found:
                run_id = found[0].info.run_id
            else:
                run = self.client.create_run(
                    self.experiment_id,
                    run_name=key.arm,
                    tags={_ARM: key.arm, _HASH: key.config_hash, "lso.level": "arm"},
                )
                run_id = run.info.run_id
                self.client.log_batch(run_id, params=[Param(k, str(v)) for k, v in params.items()])
            self._arm_runs[key.arm] = run_id
            return run_id
