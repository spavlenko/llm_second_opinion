"""The harness checkout's git state, recorded with every attempt and MLflow run."""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


@cache
def git_state() -> dict[str, str | bool | None]:
    """The checkout's commit and whether it had uncommitted changes (None outside git).
    Read once per process: a batch runs one checkout."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("rev-parse", "HEAD")
    if not commit:
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(git("status", "--porcelain"))}
