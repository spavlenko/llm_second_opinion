"""Build one arm64 task image per candidate from its repository's recipe in `tasks/repos/`.

The build context is the recipe's Dockerfile, `tasks/repos/common/`, and `src/`: the base
commit's files, with submodules at their pinned commits, checked out from local mirrors. The
image gets a fresh one-commit repository (`common/init-repo.sh`), so an agent cannot find the
fix in later history; submodules become nested one-commit repositories.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import yaml
from pydantic import Field

from llm_second_opinion.contracts import Strict
from llm_second_opinion.pipeline.candidates import Candidate

IMAGE_PREFIX = "llm-second-opinion/task"
RUN_TESTS = "/opt/lso/run-tests"
_MIRRORS = threading.Lock()  # builds run in parallel; one clone or fetch at a time


class GccRange(Strict):
    version: str
    min_pr: int | None = None
    max_pr: int | None = None

    def matches(self, number: int) -> bool:
        return (self.min_pr is None or number >= self.min_pr) and (
            self.max_pr is None or number <= self.max_pr
        )


class Recipe(Strict):
    url: str
    parser: str
    gcc: list[GccRange] = Field(min_length=1)

    def gcc_for(self, number: int) -> str:
        for r in self.gcc:
            if r.matches(number):
                return r.version
        raise ValueError(f"no GCC entry matches PR {number}")


def recipe_dir(recipes: Path, repo: str) -> Path:
    return recipes / repo.replace("/", "__")


def load_recipe(recipes: Path, repo: str) -> Recipe | None:
    path = recipe_dir(recipes, repo) / "recipe.yaml"
    return Recipe.model_validate(yaml.safe_load(path.read_text())) if path.exists() else None


def image_tag(candidate: Candidate) -> str:
    return f"{IMAGE_PREFIX}-{candidate.id.lower()}:1"


class BuildError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def build_image(
    candidate: Candidate,
    recipes: Path,
    mirrors: Path,
    log: Path,
    *,
    jobs: int,
    timeout_s: float,
) -> dict:
    """Build the candidate's image and return its build record; raises BuildError."""
    recipe = load_recipe(recipes, candidate.repo)
    if recipe is None:
        raise BuildError("no_recipe", f"no {recipe_dir(recipes, candidate.repo)}/recipe.yaml")
    gcc = recipe.gcc_for(candidate.number)
    tag = image_tag(candidate)
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lso-build-") as tmp:
        ctx = Path(tmp)
        shutil.copy(recipe_dir(recipes, candidate.repo) / "Dockerfile", ctx / "Dockerfile")
        shutil.copytree(recipes / "common", ctx / "common")
        try:
            mirror = ensure_commit(mirrors, recipe.url, candidate.base_commit)
            base_date = _git(mirror, "show", "-s", "--format=%cI", candidate.base_commit).strip()
            submodules = checkout(mirrors, mirror, candidate.base_commit, ctx / "src")
        except subprocess.CalledProcessError as e:
            raise BuildError("source_failed", f"{' '.join(e.cmd)}: {e.stderr or ''}".strip())
        # Deepest first, so a nested repository exists before its parent commits it.
        paths = sorted(submodules, key=lambda p: -p.count("/"))
        (ctx / "common/submodules").write_text("".join(f"{p}\n" for p in paths))
        iidfile = ctx / "iid"
        cmd = [
            # Without provenance (a timestamped attestation), a rebuild that Docker serves
            # entirely from cache keeps the image ID the manifest pins.
            "docker", "build", "--provenance=false", "-t", tag, "--iidfile", str(iidfile),
            "--build-arg", f"GCC={gcc}", "--build-arg", f"JOBS={jobs}",
            "--progress", "plain", str(ctx),
        ]  # fmt: skip
        start = time.monotonic()
        with log.open("w") as out:
            try:
                done = subprocess.run(
                    cmd, stdout=out, stderr=subprocess.STDOUT, timeout=timeout_s, check=False
                )
            except subprocess.TimeoutExpired:
                raise BuildError("build_timeout", f"over {timeout_s / 60:.0f} min") from None
        seconds = round(time.monotonic() - start, 1)
        if done.returncode != 0:
            raise BuildError("build_failed", f"see {log}")
        image_id = iidfile.read_text().strip()
    return {
        "image": tag,
        "image_id": image_id,
        "gcc": gcc,
        "parser": recipe.parser,
        "base_date": base_date,
        "build_s": seconds,
    }


def ensure_commit(mirrors: Path, url: str, commit: str) -> Path:
    """A bare mirror of `url` under `mirrors/` that contains `commit`."""
    name = url.rstrip("/").removesuffix(".git").split("://")[-1].removeprefix("github.com/")
    name = name.lstrip("/")  # a local path (tests)
    path = mirrors / f"{name}.git"
    with _MIRRORS:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--mirror", url, str(path)], check=True)
        if not _has_commit(path, commit):
            _git(path, "fetch", "-q", "origin", commit)
    return path


def checkout(mirrors: Path, mirror: Path, commit: str, dest: Path) -> list[str]:
    """Every tracked file at `commit` (unlike `git archive`, ignoring export-ignore), and each
    submodule's files at its recorded commit, recursively. Returns the submodule paths."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lso-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        subprocess.run(
            ["git", "-c", "core.bare=false", f"--git-dir={mirror}", f"--work-tree={dest}"]
            + ["checkout", "-f", commit, "--", "."],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    paths = []
    links = _gitlinks(mirror, commit)
    urls = _submodule_urls(mirror, commit) if links else {}
    for path, sub_commit in links:
        url = urls.get(path)
        if url is None:
            raise subprocess.CalledProcessError(1, ["submodule", path], stderr="no URL")
        sub = ensure_commit(mirrors, url, sub_commit)
        nested = checkout(mirrors, sub, sub_commit, dest / path)
        paths += [path] + [f"{path}/{n}" for n in nested]
    return paths


def _gitlinks(mirror: Path, commit: str) -> list[tuple[str, str]]:
    """(path, commit) for each submodule recorded in `commit`'s tree."""
    out = _git(mirror, "ls-tree", "-r", "-z", commit)
    links = []
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "commit":
            links.append((path, sha))
    return links


def _submodule_urls(mirror: Path, commit: str) -> dict[str, str]:
    probe = ["git", f"--git-dir={mirror}", "config", "--blob", f"{commit}:.gitmodules"]
    out = subprocess.run(
        [*probe, "--get-regexp", r"^submodule\..*\.(path|url)$"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    fields: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        key, value = line.split(" ", 1)
        name, field = key.removeprefix("submodule.").rsplit(".", 1)
        fields.setdefault(name, {})[field] = value
    return {f["path"]: f["url"] for f in fields.values() if "path" in f and "url" in f}


def _has_commit(mirror: Path, commit: str) -> bool:
    probe = ["git", f"--git-dir={mirror}", "cat-file", "-e", f"{commit}^{{commit}}"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def _git(mirror: Path, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={mirror}", *args], check=True, capture_output=True, text=True
    ).stdout
