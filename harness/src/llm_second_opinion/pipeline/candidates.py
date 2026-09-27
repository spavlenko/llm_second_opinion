"""Candidates: imported benchmark instances before images are built and the gold patch validated."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import yaml
from pydantic import Field

from llm_second_opinion.contracts import Strict
from llm_second_opinion.tasks import BlockDumper


class Candidate(Strict):
    id: str
    repo: str = Field(description="`org/name`, which selects the recipe in `tasks/repos/`.")
    number: int = Field(description="Pull request number; recipes pick a toolchain by it.")
    base_commit: str
    problem_statement: str
    test_patch: str
    gold_patch: str
    # The upstream test lists, from upstream's (x86-64) runs. Validation re-derives the task's
    # lists on arm64 and requires every upstream fail-to-pass test to pass with the gold patch.
    fail_to_pass: list[str]
    pass_to_pass: list[str] = Field(default_factory=list)
    difficulty: str | None = None


class CandidateSet(Strict):
    source: str = Field(description="Dataset and pinned revision.")
    candidates: list[Candidate]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        path.write_text(yaml.dump(data, Dumper=BlockDumper, sort_keys=False, allow_unicode=True))

    @classmethod
    def load(cls, path: Path) -> CandidateSet:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Records:
    """Per-candidate results of a pipeline stage (`builds.json`, `validation.json`), saved on
    every update so an interrupted stage resumes where it stopped."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, record: dict) -> None:
        with self._lock:
            self.data[key] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            tmp.replace(self.path)
