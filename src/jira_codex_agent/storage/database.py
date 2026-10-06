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
    EXCLUDED = "excluded"
    INTERRUPTED = "interrupted"
    RESUME_PENDING = "resume_pending"
    DONE = "done"


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
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_key TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events_issue ON task_events(issue_key,id);
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
            columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'progress' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN progress TEXT')

    def recover_interrupted(self) -> None:
        # Older daemon versions only persisted the session on completion.
        # Recover only an unambiguous session with this exact worktree.
        sessions_root = Path('~/.codex/sessions').expanduser()
        with self.connect() as db:
            for row in db.execute("SELECT issue_key,worktree FROM tasks WHERE state='coding' AND session_id IS NULL AND worktree IS NOT NULL").fetchall():
                candidates = set()
                for path in sessions_root.glob('**/*.jsonl'):
                    try:
                        with path.open() as stream:
                            metadata = json.loads(stream.readline()).get('payload', {})
                        if metadata.get('cwd') == row['worktree'] and metadata.get('id'):
                            candidates.add(metadata['id'])
                    except (OSError, ValueError):
                        continue
                if len(candidates) == 1:
                    db.execute('UPDATE tasks SET session_id=? WHERE issue_key=?', (candidates.pop(), row['issue_key']))
            db.execute("UPDATE tasks SET state='interrupted', error='Daemon stopped before completion', updated_at=? WHERE state='coding'", (datetime.now(timezone.utc).isoformat(),))

    def append_event(self, issue_key: str, kind: str, message: str) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO task_events(issue_key,timestamp,kind,message) VALUES (?,?,?,?)',
                       (issue_key, datetime.now(timezone.utc).isoformat(), kind, message[:32000]))
            db.execute('DELETE FROM task_events WHERE issue_key=? AND id NOT IN (SELECT id FROM task_events WHERE issue_key=? ORDER BY id DESC LIMIT 2000)', (issue_key, issue_key))

    def events(self, issue_key: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_events WHERE issue_key=? AND id>? ORDER BY id LIMIT 200', (issue_key, after))]

    def request_resume(self, issue_key: str) -> None:
        with self.connect() as db:
            cursor = db.execute("UPDATE tasks SET state='resume_pending' WHERE issue_key=? AND state IN ('interrupted','failed')", (issue_key,))
            if cursor.rowcount != 1:
                raise ValueError('Task must be interrupted or failed to resume')

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
        allowed = {"worktree", "session_id", "result", "error", "progress"}
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
        else:
            query += " WHERE state != 'excluded'"
        query += " ORDER BY created_at"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def reconcile_queue(self, issues: list[tuple[str, str]]) -> None:
        """Archive queued issues outside a complete JQL snapshot; preserve work history."""
        now = datetime.now(timezone.utc).isoformat()
        keys = {key for key, _ in issues}
        with self.connect() as db:
            for row in db.execute("SELECT issue_key FROM tasks WHERE state = 'queued'").fetchall():
                if row[0] not in keys:
                    db.execute("UPDATE tasks SET state='excluded', updated_at=? WHERE issue_key=?", (now, row[0]))
            for key, summary in issues:
                db.execute(
                    """INSERT INTO tasks(issue_key,summary,state,created_at,updated_at)
                    VALUES (?,?,'queued',?,?) ON CONFLICT(issue_key) DO UPDATE SET
                    summary=excluded.summary, updated_at=excluded.updated_at,
                    state=CASE WHEN tasks.state='excluded' THEN 'queued' ELSE tasks.state END""",
                    (key, summary, now, now),
                )

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
