import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# `bench` loads the repo's .env (endpoints, secrets); tests must not depend on it.
os.environ["BENCH_ENV_FILE"] = str(REPO / "harness/tests/no.env")


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO
