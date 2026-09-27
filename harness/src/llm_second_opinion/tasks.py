"""The internal task format and frozen manifests.

Manifests are written by the task pipeline (`pipeline/`) or, for the toy set, by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from llm_second_opinion.contracts import Strict
from llm_second_opinion.testlogs import PARSERS


class TestLists(Strict):
    """Per-test grading: `eval_command`'s output is parsed, and every listed test must pass."""

    __test__ = False  # not a pytest class

    parser: str = Field(description="A name in `testlogs.PARSERS`.")
    fail_to_pass: list[str] = Field(min_length=1)
    pass_to_pass: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_parser(self) -> TestLists:
        if self.parser not in PARSERS:
            raise ValueError(f"unknown parser {self.parser!r}; known: {', '.join(PARSERS)}")
        return self


class Task(Strict):
    id: str = Field(min_length=1)
    image: str = Field(description="Image with the repo checked out at the base commit.")
    image_id: str | None = Field(None, description="Local image ID the manifest was validated on.")
    workdir: str = Field("/testbed", description="Git repo inside the image.")
    problem_statement: str
    test_patch: str = Field("", description="Applied only at grading; the agent never sees it.")
    gold_patch: str = Field("", description="Reference fix, used by the `gold` agent.")
    eval_command: str = Field(
        description="Run in workdir at grading. Without `tests`, exit 0 means resolved."
    )
    tests: TestLists | None = None
    repo: str | None = Field(None, description="`org/name` upstream.")
    base_commit: str | None = None
    base_date: str | None = Field(None, description="Commit date of the base commit (ISO 8601).")
    split: Literal["dev", "test"] | None = None


class Dropped(Strict):
    id: str
    reason: str
    detail: str = ""


class Manifest(Strict):
    version: str
    source: str = Field("", description="Where the tasks came from, with a pinned revision.")
    tasks: list[Task] = Field(min_length=1)
    dropped: list[Dropped] = Field(default_factory=list)

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

    def to_yaml(self, path: Path, header: str = "") -> None:
        data = self.model_dump(mode="json", exclude_defaults=True)
        text = yaml.dump(data, Dumper=BlockDumper, sort_keys=False, allow_unicode=True, width=100)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + text)


class BlockDumper(yaml.SafeDumper):
    """Multi-line strings (patches, issue text) as literal blocks, so manifests diff well.

    PyYAML falls back to a quoted scalar when a block cannot represent the string exactly.
    """


BlockDumper.add_representer(
    str,
    lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None),
)
