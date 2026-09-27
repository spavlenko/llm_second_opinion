import pytest
from pydantic import ValidationError

from llm_second_opinion.tasks import Manifest


def test_toy_manifest_loads(repo):
    manifest = Manifest.from_yaml(repo / "tasks/manifests/toy-v1.yaml")
    assert [t.id for t in manifest.tasks] == ["toy-add", "toy-max"]
    assert all(t.gold_patch and t.test_patch for t in manifest.tasks)


def test_duplicate_task_ids_are_rejected():
    task = {"id": "t", "image": "i", "problem_statement": "p", "eval_command": "true"}
    with pytest.raises(ValidationError, match="duplicate task ids: t"):
        Manifest.model_validate({"version": "1", "tasks": [task, task]})
