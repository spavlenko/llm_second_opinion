"""The internal task format and frozen manifests.

Importers (Multi-SWE-bench, SWE-bench-Live) will write manifests in this format; until
they exist, manifests are written by hand.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, model_validator

from llm_second_opinion.contracts import Strict


class Task(Strict):
    id: str = Field(min_length=1)
    image: str = Field(description="Image with the repo checked out at the base commit.")
    workdir: str = Field("/testbed", description="Git repo inside the image.")
    problem_statement: str
    test_patch: str = Field("", description="Applied only at grading; the agent never sees it.")
    gold_patch: str = Field("", description="Reference fix, used by the `gold` agent.")
    eval_command: str = Field(description="Run in workdir at grading; exit 0 means resolved.")


class Manifest(Strict):
    version: str
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Manifest:
        ids = [t.id for t in self.tasks]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids: {', '.join(duplicates)}")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> Manifest:
        return cls.model_validate(yaml.safe_load(path.read_text()))
