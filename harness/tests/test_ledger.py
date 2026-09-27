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
