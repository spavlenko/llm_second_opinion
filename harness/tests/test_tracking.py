import pytest

pytest.importorskip("mlflow")

from llm_second_opinion.ledger import ItemKey
from llm_second_opinion.tracking import Tracker


def test_items_are_child_runs_of_one_run_per_arm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a SQLite store puts artifacts in ./mlruns
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    artifacts = tmp_path / "item"
    artifacts.mkdir()
    (artifacts / "patch.diff").write_text("diff")

    tracker = Tracker(uri, "exp")
    ids = [
        tracker.log_item(
            ItemKey("exp", "A0", "t", seed, "h1"),
            arm_params={"agent": "gold"},
            params={"seed": seed},
            metrics={"resolved": 1.0},
            artifacts=artifacts,
        )
        for seed in (0, 1)
    ]
    tracker.close()
    # A resumed batch reuses the arm's run while the config hash is unchanged.
    resumed = Tracker(uri, "exp")
    resumed.log_item(ItemKey("exp", "A0", "t", 2, "h1"), {}, {}, {}, artifacts)
    resumed.close()

    client = tracker.client
    parents = {client.get_run(i).data.tags["mlflow.parentRunId"] for i in ids}
    assert len(parents) == 1
    arm_run = client.get_run(parents.pop())
    assert arm_run.data.params == {"agent": "gold"}
    assert arm_run.info.status == "FINISHED"
    assert [a.path for a in client.list_artifacts(ids[0])] == ["patch.diff"]
    runs = client.search_runs([tracker.experiment_id])
    assert len(runs) == 4  # one arm run, three items
