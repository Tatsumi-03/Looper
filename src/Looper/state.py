"""SQLite-backed task state. Single source of truth; survives daemon restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class State(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    WORKTREE = "WORKTREE"
    SOLVING = "SOLVING"
    PUSHED = "PUSHED"
    PR_OPEN = "PR_OPEN"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    SCORED = "SCORED"
    REVISING = "REVISING"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    PARKED = "PARKED"


TERMINAL = {State.READY_FOR_HUMAN, State.PARKED}
#: states from which a restarted daemon re-enters the review loop rather than re-solving
POST_PR = {State.PR_OPEN, State.AWAITING_REVIEW, State.SCORED, State.REVISING, State.PUSHED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    issue_number INTEGER PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,
    branch       TEXT,
    pr_number    INTEGER,
    head_sha     TEXT,
    pushed_at    TEXT,
    session_id   TEXT,
    iteration    INTEGER NOT NULL DEFAULT 0,
    last_score   INTEGER,
    best_score   INTEGER,
    retriggers   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    iteration    INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    session_id   TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    ok           INTEGER,
    num_turns    INTEGER,
    cost_usd     REAL,
    result_text  TEXT,
    log_path     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    state        TEXT,
    message      TEXT NOT NULL,
    data         TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_number, id);
CREATE INDEX IF NOT EXISTS idx_attempts_issue ON attempts(issue_number, id);
"""

TASK_COLUMNS = (
    "issue_number title state branch pr_number head_sha pushed_at session_id iteration "
    "last_score best_score retriggers cost_usd error created_at updated_at"
).split()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    issue_number: int
    title: str = ""
    state: str = State.PENDING
    branch: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    pushed_at: str | None = None
    session_id: str | None = None
    iteration: int = 0
    last_score: int | None = None
    best_score: int | None = None
    retriggers: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- tasks -----------------------------------------------------------
    def get(self, issue_number: int) -> Task | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE issue_number = ?", (issue_number,)
            ).fetchone()
        return Task(**dict(row)) if row else None

    def create(self, issue_number: int, title: str) -> Task:
        ts = now()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO tasks (issue_number, title, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (issue_number, title, State.PENDING, ts, ts),
            )
            self._db.commit()
        task = self.get(issue_number)
        assert task is not None
        return task

    def update(self, issue_number: int, **fields: Any) -> Task:
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task columns: {sorted(unknown)}")
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE tasks SET {assignments} WHERE issue_number = ?",
                (*fields.values(), issue_number),
            )
            self._db.commit()
        task = self.get(issue_number)
        if task is None:
            raise KeyError(f"no task for issue #{issue_number}")
        return task

    def set_state(self, issue_number: int, state: State, message: str = "", **fields: Any) -> Task:
        task = self.update(issue_number, state=str(state), **fields)
        self.event(issue_number, message or f"-> {state}", state=state, data=fields or None)
        return task

    def all_tasks(self) -> list[Task]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY issue_number").fetchall()
        return [Task(**dict(r)) for r in rows]

    def unfinished(self) -> list[Task]:
        return [t for t in self.all_tasks() if not t.is_terminal]

    def open_pr_count(self) -> int:
        return sum(1 for t in self.all_tasks() if t.pr_number and t.state != State.PARKED
                   and t.state != State.READY_FOR_HUMAN)

    def add_cost(self, issue_number: int, usd: float) -> float:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET cost_usd = cost_usd + ?, updated_at = ? WHERE issue_number = ?",
                (usd, now(), issue_number),
            )
            self._db.commit()
        task = self.get(issue_number)
        return task.cost_usd if task else 0.0

    def record_score(self, issue_number: int, score: int) -> Task:
        task = self.get(issue_number)
        best = score if task is None or task.best_score is None else max(task.best_score, score)
        return self.update(issue_number, last_score=score, best_score=best)

    # --- attempts --------------------------------------------------------
    def start_attempt(self, issue_number: int, iteration: int, kind: str, session_id: str | None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO attempts (issue_number, iteration, kind, session_id, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (issue_number, iteration, kind, session_id, now()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def finish_attempt(self, attempt_id: int, *, ok: bool, num_turns: int | None,
                       cost_usd: float | None, result_text: str | None,
                       log_path: str | None, session_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE attempts SET ended_at = ?, ok = ?, num_turns = ?, cost_usd = ?,"
                " result_text = ?, log_path = ?, session_id = COALESCE(?, session_id)"
                " WHERE id = ?",
                (now(), int(ok), num_turns, cost_usd, result_text, log_path, session_id, attempt_id),
            )
            self._db.commit()

    def attempts(self, issue_number: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM attempts WHERE issue_number = ? ORDER BY id", (issue_number,)
            ).fetchall()

    # --- events ----------------------------------------------------------
    def event(self, issue_number: int, message: str, *, state: State | str | None = None,
              data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (issue_number, ts, state, message, data) VALUES (?, ?, ?, ?, ?)",
                (issue_number, now(), str(state) if state else None, message,
                 json.dumps(data, default=str) if data else None),
            )
            self._db.commit()

    def events(self, issue_number: int, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM events WHERE issue_number = ? ORDER BY id DESC LIMIT ?",
                (issue_number, limit),
            ).fetchall()
