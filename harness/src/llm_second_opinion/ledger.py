"""The run ledger (SQLite): one row per work item for resume and reports, one row per attempt
for spend and failures, one row per batch, and the usage preflight calls."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLES = ("executor", "advisor")
TOKEN_KINDS = ("prompt", "completion", "cached", "reasoning")
# Metered spend, on items, attempts, and preflight rows; null when not metered (or, for
# cost_usd, when a model that made calls has no price).
SPEND_COLUMNS = {
    **{f"{role}_{kind}_tokens": "INTEGER" for role in ROLES for kind in TOKEN_KINDS},
    "model_calls": "INTEGER",  # calls with a 2xx answer
    "failed_calls": "INTEGER",  # calls with an error status or no answer (not billed)
    "cost_usd": "REAL",
}
_KEY = ("experiment", "arm", "task", "seed", "config_hash", "image_id")

_ITEMS = {
    "experiment": "TEXT NOT NULL",
    "arm": "TEXT NOT NULL",
    "task": "TEXT NOT NULL",
    "seed": "INTEGER NOT NULL",
    "config_hash": "TEXT NOT NULL",
    # The task image the item ran on: a rebuilt image is a different item. '' in rows from
    # ledgers written before it was recorded.
    "image_id": "TEXT NOT NULL DEFAULT ''",
    "status": "TEXT NOT NULL",  # running, done, failed, interrupted
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "resolved": "INTEGER",
    "grade": "TEXT",
    "exit_reason": "TEXT",
    "turns": "INTEGER",
    "duration_s": "REAL",
    "error": "TEXT",
    "mlflow_run_id": "TEXT",
    "updated": "REAL NOT NULL",
    **SPEND_COLUMNS,
    "prompt_hash": "TEXT",
    "level": "TEXT",
    "interventions": "TEXT",  # comma-separated
    "f2p_passed": "INTEGER",
    "f2p_total": "INTEGER",
    "p2p_passed": "INTEGER",
    "p2p_total": "INTEGER",
    "build_failed": "INTEGER",
    "edited_tests": "INTEGER",  # files the agent changed in tests (grade.json lists them)
    # The counted attempt's directory, relative to runs/<experiment>/.
    "attempt_dir": "TEXT",
}
# One row per attempt, whatever its outcome: what was spent, and why it failed.
_ATTEMPTS = {
    "experiment": "TEXT NOT NULL",
    "arm": "TEXT NOT NULL",
    "task": "TEXT NOT NULL",
    "seed": "INTEGER NOT NULL",
    "config_hash": "TEXT NOT NULL",
    "image_id": "TEXT NOT NULL",
    "attempt": "INTEGER NOT NULL",  # attempt-<k> in the item's directory
    "dir": "TEXT NOT NULL",  # relative to runs/<experiment>/
    "started": "REAL NOT NULL",
    "agent_ended": "REAL",  # set once the agent's result is on disk
    "ended": "REAL",
    "status": "TEXT NOT NULL",  # running, done, failed, interrupted
    "error": "TEXT",
    "exit_reason": "TEXT",
    **SPEND_COLUMNS,
}
# One row per `bench run` batch, so a report can say whether test tasks were run with --final.
_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    experiment TEXT NOT NULL,
    started REAL NOT NULL,
    split TEXT,                    -- the experiment's split; NULL means all tasks
    test_tasks INTEGER NOT NULL,   -- test-split tasks among those selected
    final INTEGER NOT NULL         -- 1 if run with --final
)
"""
# The usage preflight's calls, one row per endpoint and batch: part of the total spend.
_PREFLIGHT = {
    "experiment": "TEXT NOT NULL",
    "started": "REAL NOT NULL",
    "model_key": "TEXT NOT NULL",
    "model": "TEXT NOT NULL",
    "calls": "INTEGER NOT NULL",
    "failed_calls": "INTEGER NOT NULL",
    "prompt_tokens": "INTEGER NOT NULL",
    "completion_tokens": "INTEGER NOT NULL",
    "cached_tokens": "INTEGER NOT NULL",
    "reasoning_tokens": "INTEGER NOT NULL",
    "cost_usd": "REAL",
}
# The adapter fingerprint each arm ran with (see `Experiment.config_hash`), per batch.
_FINGERPRINTS = {
    "experiment": "TEXT NOT NULL",
    "arm": "TEXT NOT NULL",
    "recorded": "REAL NOT NULL",
    "fingerprint": "TEXT NOT NULL",  # JSON
}


def _create(table: str, columns: dict[str, str], key: tuple[str, ...] = ()) -> str:
    body = ",\n    ".join(f"{name} {kind}" for name, kind in columns.items())
    if key:
        body += f",\n    PRIMARY KEY ({', '.join(key)})"
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {body}\n)"


@dataclass(frozen=True)
class ItemKey:
    experiment: str
    arm: str
    task: str
    seed: int
    config_hash: str
    image_id: str = ""

    def __str__(self) -> str:
        return f"{self.arm}/{self.task}/seed-{self.seed}"

    def dir(self) -> Path:
        """The item's directory under runs/<experiment>/; attempts are directories in it."""
        return Path(self.arm) / self.task / f"seed-{self.seed}" / self.config_hash


class Ledger:
    """Thread-safe: workers share one connection behind a lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()
        self._db.execute(_create("items", _ITEMS, _KEY))
        self._db.execute(_create("attempts", _ATTEMPTS, (*_KEY, "attempt")))
        self._db.execute(_SESSIONS)
        self._db.execute(_create("preflight", _PREFLIGHT))
        self._db.execute(_create("fingerprints", _FINGERPRINTS))

    def _migrate(self) -> None:
        """Bring an older ledger's items table to the current schema in place, so a resume
        keeps its rows: missing columns are added, and a table without `image_id` in its
        key is rebuilt (its rows get image_id '')."""
        existing = [row["name"] for row in self._db.execute("PRAGMA table_info(items)")]
        if not existing:
            return
        if "image_id" not in existing:
            common = [c for c in existing if c in _ITEMS]
            names = ", ".join(common)
            self._db.execute("BEGIN")
            self._db.execute("ALTER TABLE items RENAME TO items_old")
            self._db.execute(_create("items", _ITEMS, _KEY))
            self._db.execute(f"INSERT INTO items ({names}) SELECT {names} FROM items_old")
            self._db.execute("DROP TABLE items_old")
            self._db.execute("COMMIT")
            return
        for name, kind in _ITEMS.items():
            if name not in existing:
                self._db.execute(f"ALTER TABLE items ADD COLUMN {name} {kind}")

    # --- items -----------------------------------------------------------------------

    def status(self, key: ItemKey) -> str | None:
        row = self.item(key)
        return row["status"] if row else None

    def item(self, key: ItemKey) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(f"SELECT * FROM items WHERE {_WHERE}", _values(key)).fetchone()
        return dict(row) if row else None

    def start(self, key: ItemKey) -> int:
        """Mark the item running and return its attempt count, from 1. The previous
        attempt's error stays in its `attempts` row."""
        with self._lock:
            self._db.execute(
                f"INSERT INTO items ({', '.join(_KEY)}, status, attempts, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', 1, ?) "
                "ON CONFLICT DO UPDATE SET status='running', attempts=attempts+1, error=NULL, "
                "updated=excluded.updated",
                (*_values(key), time.time()),
            )
            row = self._db.execute(
                f"SELECT attempts FROM items WHERE {_WHERE}", _values(key)
            ).fetchone()
        return row["attempts"]

    def finish(self, key: ItemKey, status: str, **fields: Any) -> None:
        columns = ["status", "updated", *fields]
        assignments = ", ".join(f"{c}=?" for c in columns)
        with self._lock:
            self._db.execute(
                f"UPDATE items SET {assignments} WHERE {_WHERE}",
                (status, time.time(), *fields.values(), *_values(key)),
            )

    def rows(self, experiment: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE experiment=? ORDER BY arm, task, seed", (experiment,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- attempts --------------------------------------------------------------------

    def new_attempt(self, key: ItemKey, runs_dir: Path) -> tuple[int, Path]:
        """Number and register a new attempt; returns (k, its directory relative to
        runs_dir). k follows every attempt already in the item's directory, whatever its
        image, and any directory left there by a ledger that was since deleted, so no
        attempt ever reuses another's directory."""
        item_dir = key.dir()
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(attempt) AS k FROM attempts WHERE experiment=? AND arm=? AND task=? "
                "AND seed=? AND config_hash=?",
                _values(key)[:5],
            ).fetchone()
            on_disk = [
                int(p.name.removeprefix("attempt-"))
                for p in (runs_dir / item_dir).glob("attempt-*")
                if p.name.removeprefix("attempt-").isdigit()
            ]
            k = max([row["k"] or 0, *on_disk]) + 1
            rel = item_dir / f"attempt-{k}"
            self._db.execute(
                f"INSERT INTO attempts ({', '.join(_KEY)}, attempt, dir, started, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
                (*_values(key), k, str(rel), time.time()),
            )
        return k, rel

    def update_attempt(self, key: ItemKey, attempt: int, **fields: Any) -> None:
        assignments = ", ".join(f"{c}=?" for c in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE attempts SET {assignments} WHERE {_WHERE} AND attempt=?",
                (*fields.values(), *_values(key), attempt),
            )

    def attempts(self, experiment: str, key: ItemKey | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM attempts WHERE experiment=?", (experiment,)
        if key is not None:
            query, args = f"SELECT * FROM attempts WHERE {_WHERE}", _values(key)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY arm, task, seed, attempt", args).fetchall()
        return [dict(r) for r in rows]

    def resumable(self, key: ItemKey) -> dict[str, Any] | None:
        """The item's latest attempt if its agent finished but grading or tracking did not:
        it is graded again from the result on disk instead of running the agent again."""
        rows = self.attempts(key.experiment, key)
        last = rows[-1] if rows else None
        if last and last["agent_ended"] is not None and last["status"] != "done":
            return last
        return None

    def interrupted(self, experiment: str) -> list[dict[str, Any]]:
        """Mark attempts and items left `running` by a process that died as `interrupted`,
        and return those attempts (their spend is still to be read from their directories)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM attempts WHERE experiment=? AND status='running'", (experiment,)
            ).fetchall()
            self._db.execute(
                "UPDATE attempts SET status='interrupted', ended=COALESCE(ended, ?) "
                "WHERE experiment=? AND status='running'",
                (time.time(), experiment),
            )
            self._db.execute(
                "UPDATE items SET status='interrupted', updated=? "
                "WHERE experiment=? AND status='running'",
                (time.time(), experiment),
            )
        return [dict(r) for r in rows]

    # --- batches and preflight -------------------------------------------------------

    def record_session(
        self, experiment: str, split: str | None, test_tasks: int, final: bool
    ) -> float:
        started = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (experiment, started, split, test_tasks, final) "
                "VALUES (?, ?, ?, ?, ?)",
                (experiment, started, split, test_tasks, int(final)),
            )
        return started

    def sessions(self, experiment: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE experiment=? ORDER BY started", (experiment,)
            ).fetchall()
        return [dict(r) for r in rows]

    def record_fingerprints(self, experiment: str, fingerprints: dict[str, dict]) -> None:
        """Each arm's adapter fingerprint (in its config hash) as a batch used it, so a
        report can tell the current hashes without building the agent bundle."""
        with self._lock:
            self._db.executemany(
                "INSERT INTO fingerprints (experiment, arm, recorded, fingerprint) "
                "VALUES (?, ?, ?, ?)",
                [
                    (experiment, arm, time.time(), json.dumps(fp, sort_keys=True))
                    for arm, fp in fingerprints.items()
                ],
            )

    def fingerprints(self, experiment: str) -> dict[str, dict]:
        """The latest fingerprint recorded for each arm."""
        with self._lock:
            rows = self._db.execute(
                "SELECT arm, fingerprint FROM fingerprints WHERE experiment=? ORDER BY recorded",
                (experiment,),
            ).fetchall()
        return {r["arm"]: json.loads(r["fingerprint"]) for r in rows}

    def record_preflight(self, experiment: str, started: float, **fields: Any) -> None:
        names = ["experiment", "started", *fields]
        with self._lock:
            self._db.execute(
                f"INSERT INTO preflight ({', '.join(names)}) "
                f"VALUES ({', '.join('?' * len(names))})",
                (experiment, started, *fields.values()),
            )

    def preflight(self, experiment: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM preflight WHERE experiment=? ORDER BY started", (experiment,)
            ).fetchall()
        return [dict(r) for r in rows]


_WHERE = " AND ".join(f"{c}=?" for c in _KEY)


def _values(key: ItemKey) -> tuple:
    return (key.experiment, key.arm, key.task, key.seed, key.config_hash, key.image_id)
