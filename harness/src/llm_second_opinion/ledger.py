"""The run ledger: one SQLite row per work item, used for resume and reports."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    experiment TEXT NOT NULL,
    arm TEXT NOT NULL,
    task TEXT NOT NULL,
    seed INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,          -- running, done, failed
    attempts INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER,
    grade TEXT,
    exit_reason TEXT,
    turns INTEGER,
    duration_s REAL,
    error TEXT,
    mlflow_run_id TEXT,
    updated REAL NOT NULL,
    PRIMARY KEY (experiment, arm, task, seed, config_hash)
)
"""
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

# Added after the first ledgers were written; null when an item was not metered (or, for
# cost_usd, when a model it used has no price).
_ADDED_COLUMNS = {
    "executor_prompt_tokens": "INTEGER",
    "executor_completion_tokens": "INTEGER",
    "advisor_prompt_tokens": "INTEGER",
    "advisor_completion_tokens": "INTEGER",
    "model_calls": "INTEGER",
    "cost_usd": "REAL",
}


@dataclass(frozen=True)
class ItemKey:
    experiment: str
    arm: str
    task: str
    seed: int
    config_hash: str

    def __str__(self) -> str:
        return f"{self.arm}/{self.task}/seed-{self.seed}"


class Ledger:
    """Thread-safe: workers share one connection behind a lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_SCHEMA)
        self._db.execute(_SESSIONS)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Add columns that older ledgers lack, so a resume keeps their rows."""
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(items)")}
        for name, kind in _ADDED_COLUMNS.items():
            if name not in existing:
                self._db.execute(f"ALTER TABLE items ADD COLUMN {name} {kind}")

    def status(self, key: ItemKey) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM items WHERE experiment=? AND arm=? AND task=? AND seed=? "
                "AND config_hash=?",
                _values(key),
            ).fetchone()
        return row["status"] if row else None

    def start(self, key: ItemKey) -> int:
        """Mark the item running and return its attempt number, from 1."""
        with self._lock:
            self._db.execute(
                "INSERT INTO items (experiment, arm, task, seed, config_hash, status, attempts, "
                "updated) VALUES (?, ?, ?, ?, ?, 'running', 1, ?) "
                "ON CONFLICT DO UPDATE SET status='running', attempts=attempts+1, error=NULL, "
                "updated=excluded.updated",
                (*_values(key), time.time()),
            )
            row = self._db.execute(
                "SELECT attempts FROM items WHERE experiment=? AND arm=? AND task=? AND seed=? "
                "AND config_hash=?",
                _values(key),
            ).fetchone()
        return row["attempts"]

    def finish(self, key: ItemKey, status: str, **fields: Any) -> None:
        columns = ["status", "updated", *fields]
        assignments = ", ".join(f"{c}=?" for c in columns)
        with self._lock:
            self._db.execute(
                f"UPDATE items SET {assignments} WHERE experiment=? AND arm=? AND task=? "
                "AND seed=? AND config_hash=?",
                (status, time.time(), *fields.values(), *_values(key)),
            )

    def rows(self, experiment: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE experiment=? ORDER BY arm, task, seed", (experiment,)
            ).fetchall()
        return [dict(r) for r in rows]

    def record_session(
        self, experiment: str, split: str | None, test_tasks: int, final: bool
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (experiment, started, split, test_tasks, final) "
                "VALUES (?, ?, ?, ?, ?)",
                (experiment, time.time(), split, test_tasks, int(final)),
            )

    def sessions(self, experiment: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE experiment=? ORDER BY started", (experiment,)
            ).fetchall()
        return [dict(r) for r in rows]


def _values(key: ItemKey) -> tuple:
    return (key.experiment, key.arm, key.task, key.seed, key.config_hash)
