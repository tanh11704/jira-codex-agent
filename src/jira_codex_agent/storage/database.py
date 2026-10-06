from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator


class TaskState(StrEnum):
    QUEUED = "queued"
    CODING = "coding"
    REVIEW = "review"
    FAILED = "failed"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    issue_key TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    state TEXT NOT NULL,
                    worktree TEXT,
                    session_id TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            db.execute("INSERT OR IGNORE INTO agent_state VALUES ('paused', 'false')")

    def upsert_task(self, issue_key: str, summary: str, state: TaskState = TaskState.QUEUED) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO tasks(issue_key, summary, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(issue_key) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at""",
                (issue_key, summary, state.value, now, now),
            )

    def update_task(self, issue_key: str, state: TaskState, **fields: object) -> None:
        allowed = {"worktree", "session_id", "result", "error"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported fields: {sorted(invalid)}")
        values = {key: json.dumps(value) if key == "result" and value is not None else value for key, value in fields.items()}
        values.update(state=state.value, updated_at=datetime.now(timezone.utc).isoformat())
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE tasks SET {assignments} WHERE issue_key = ?",  # keys are allow-listed
                (*values.values(), issue_key),
            )

    def list_tasks(self, state: TaskState | None = None) -> list[dict]:
        query = "SELECT * FROM tasks"
        params: tuple[str, ...] = ()
        if state:
            query += " WHERE state = ?"
            params = (state.value,)
        query += " ORDER BY created_at"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def task_keys(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT issue_key FROM tasks")}

    def set_paused(self, paused: bool) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO agent_state VALUES ('paused', ?)", (json.dumps(paused),))

    def is_paused(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT value FROM agent_state WHERE key='paused'").fetchone()
            return bool(row and json.loads(row[0]))
