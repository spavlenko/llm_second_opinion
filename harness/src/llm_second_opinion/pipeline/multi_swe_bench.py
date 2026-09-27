"""Import Multi-SWE-bench instances (mini now, full later) from Hugging Face at a pinned revision."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from llm_second_opinion.pipeline.candidates import Candidate, CandidateSet


@dataclass(frozen=True)
class Dataset:
    hf_repo: str
    revision: str  # a commit, so the files cannot change under us
    files: dict[str, tuple[str, ...]]  # language -> files holding its instances
    sha256: dict[str, str]  # file -> checksum, where known

    def source(self, language: str) -> str:
        return f"{self.hf_repo}@{self.revision} ({language})"


DATASETS = {
    # 400 instances, 50 per language, in one file.
    "mini": Dataset(
        "ByteDance-Seed/Multi-SWE-bench_mini",
        "d0fab3ccc7dff232fcaac234cf8af9a2efeaccf6",
        {"c++": ("multi_swe_bench_mini.jsonl",)},
        {
            "multi_swe_bench_mini.jsonl": (
                "6644b9c9ebaf5e5b37cb9d81c4dce688c101f07436aed9d50fc55c85b164c3b2"
            )
        },
    ),
    # One file per repository; cpp-httplib has no recipe in tasks/repos/ yet.
    "full": Dataset(
        "ByteDance-Seed/Multi-SWE-bench",
        "56ff018c04a38e27ada1e9d0a6d5839a51f88f0d",
        {
            "c++": tuple(
                f"cpp/{r}_dataset.jsonl"
                for r in (
                    "catchorg__Catch2",
                    "fmtlib__fmt",
                    "nlohmann__json",
                    "simdjson__simdjson",
                    "yhirose__cpp-httplib",
                )
            )
        },
        {},
    ),
}


def import_candidates(
    dataset: str, language: str, cache: Path, instances: set[str] | None = None
) -> CandidateSet:
    ds = DATASETS[dataset]
    if language not in ds.files:
        raise ValueError(f"{dataset} has no {language!r} files; known: {', '.join(ds.files)}")
    candidates = [
        to_candidate(row)
        for row in _rows(ds, language, cache)
        if row.get("language", language) == language
        and (instances is None or row["instance_id"] in instances)
    ]
    if instances:
        missing = instances - {c.id for c in candidates}
        if missing:
            raise ValueError(f"not in {dataset} ({language}): {', '.join(sorted(missing))}")
    return CandidateSet(source=ds.source(language), candidates=candidates)


def to_candidate(row: dict) -> Candidate:
    """One dataset row. Upstream's fixed tests (fail, skip, or absent before; pass after) are
    the candidate's fail-to-pass tests."""
    fixed = {**row["f2p_tests"], **row["s2p_tests"], **row["n2p_tests"]}
    return Candidate(
        id=row["instance_id"],
        repo=f"{row['org']}/{row['repo']}",
        number=row["number"],
        base_commit=row["base"]["sha"],
        problem_statement=problem_statement(row["resolved_issues"]),
        test_patch=row["test_patch"],
        gold_patch=row["fix_patch"],
        fail_to_pass=sorted(fixed),
        pass_to_pass=sorted(row["p2p_tests"]),
        difficulty=row.get("difficulty"),
    )


def problem_statement(issues: list[dict]) -> str:
    """The resolved issues' titles and bodies. The pull request's own text is left out: it
    describes the fix."""
    parts = [f"{i['title']}\n\n{(i.get('body') or '').strip()}".strip() for i in issues]
    return "\n\n---\n\n".join(parts).replace("\r\n", "\n")


def _rows(ds: Dataset, language: str, cache: Path) -> Iterator[dict]:
    for name in ds.files[language]:
        with _fetch(ds, name, cache).open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def _fetch(ds: Dataset, name: str, cache: Path) -> Path:
    path = cache / ds.hf_repo / ds.revision / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/datasets/{ds.hf_repo}/resolve/{ds.revision}/{name}"
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        tmp.replace(path)
    expected = ds.sha256.get(name)
    if expected and _sha256(path) != expected:
        raise ValueError(f"{path}: checksum mismatch; delete it to download again")
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
