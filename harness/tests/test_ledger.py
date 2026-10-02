from dataclasses import astuple

from llm_second_opinion.ledger import ItemKey, Ledger

KEY = ItemKey("exp", "A0", "task-1", 0, "abc")


def test_attempts_count_up_and_finish_records_fields(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert ledger.status(KEY) is None
    assert ledger.start(KEY) == 1
    assert ledger.status(KEY) == "running"
    assert ledger.start(KEY) == 2
    ledger.finish(KEY, "done", resolved=True, turns=4)
    [row] = ledger.rows("exp")
    assert (row["status"], row["attempts"], row["resolved"], row["turns"]) == ("done", 2, 1, 4)


def test_a_different_config_hash_is_a_different_item(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.start(KEY)
    ledger.finish(KEY, "done")
    other = ItemKey(KEY.experiment, KEY.arm, KEY.task, KEY.seed, "def")
    assert ledger.status(other) is None


def test_a_rebuilt_task_image_is_a_different_item(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    old, new = (ItemKey(*astuple(KEY)[:5], image) for image in ("sha256:a", "sha256:b"))
    ledger.start(old)
    ledger.finish(old, "done")
    assert ledger.status(new) is None
    # Attempt directories are shared by the item's config hash, so numbering continues.
    assert ledger.new_attempt(old, tmp_path)[0] == 1
    k, rel = ledger.new_attempt(new, tmp_path)
    assert (k, str(rel)) == (2, "A0/task-1/seed-0/abc/attempt-2")


def test_running_attempts_of_a_dead_process_become_interrupted(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.start(KEY)
    ledger.new_attempt(KEY, tmp_path)
    [row] = Ledger(tmp_path / "ledger.sqlite").interrupted("exp")
    assert row["attempt"] == 1
    assert [a["status"] for a in ledger.attempts("exp")] == ["interrupted"]
    assert ledger.status(KEY) == "interrupted"
