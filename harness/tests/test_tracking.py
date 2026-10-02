import pytest

pytest.importorskip("mlflow")

from llm_second_opinion.ledger import ItemKey
from llm_second_opinion.tracing import Span
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


def test_trace_is_linked_to_the_item_run_with_recorded_times(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    artifacts = tmp_path / "item"
    artifacts.mkdir()
    tool = Span("bash", "TOOL", 1_200, 1_800, inputs={"command": "ls"}, error="exit 2")
    turn = Span("turn 1", "CHAIN", 1_100, 1_900, children=[tool])
    root = Span("A0/t/seed-0", "CHAIN", 1_000, 5_000, children=[turn])

    tracker = Tracker(uri, "exp")
    run_id = tracker.log_item(ItemKey("exp", "A0", "t", 0, "h"), {}, {}, {}, artifacts, root)
    tracker.log_arm_summary("A0", "h", {}, {"resolve_rate": 0.5})
    tracker.close()

    client = tracker.client
    [trace] = client.search_traces(locations=[tracker.experiment_id])
    assert trace.info.request_metadata["mlflow.sourceRun"] == run_id
    spans = {s.name: s for s in trace.data.spans}
    assert spans["bash"].status.status_code == "ERROR"
    assert (spans["bash"].start_time_ns, spans["bash"].end_time_ns) == (1_200, 1_800)
    assert spans["bash"].parent_id == spans["turn 1"].span_id
    arm_run = client.get_run(client.get_run(run_id).data.tags["mlflow.parentRunId"])
    assert arm_run.data.metrics["resolve_rate"] == 0.5
    assert "mlflow.source.git.commit" in arm_run.data.tags


def test_final_runs_are_tagged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    artifacts = tmp_path / "item"
    artifacts.mkdir()
    dev = Tracker(uri, "exp")
    dev_item = dev.log_item(ItemKey("exp", "A0", "t", 0, "h"), {}, {}, {}, artifacts)
    dev.close()
    final = Tracker(uri, "exp", final=True)
    test_item = final.log_item(ItemKey("exp", "A0", "u", 0, "h"), {}, {}, {}, artifacts)
    final.close()

    client = final.client
    assert "lso.final" not in client.get_run(dev_item).data.tags
    run = client.get_run(test_item)
    assert run.data.tags["lso.final"] == "true"
    assert client.get_run(run.data.tags["mlflow.parentRunId"]).data.tags["lso.final"] == "true"
