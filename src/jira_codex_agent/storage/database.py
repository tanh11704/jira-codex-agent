from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator


class TaskState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"  # Jira says a dependency is unfinished; re-checked on every sync
    CODING = "coding"
    REVIEW = "review"
    NEEDS_HUMAN = "needs_human"  # Codex finished but reported PARTIAL/BLOCKED/NEEDS_HUMAN_REVIEW
    FAILED = "failed"
    EXCLUDED = "excluded"
    SKIPPED = "skipped"  # discarded by a human; never picked up again automatically
    INTERRUPTED = "interrupted"
    RESUME_PENDING = "resume_pending"
    DONE = "done"


# Columns added after the first release, created on startup when missing.
MIGRATED_COLUMNS = {
    "progress": "TEXT",
    "description": "TEXT",
    "issue_type": "TEXT",
    "priority": "TEXT",
    "blocked_reason": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at": "TEXT",
    "report": "TEXT",
    "retry_note": "TEXT",
    "prompt": "TEXT",
    "plan": "TEXT",  # Claude's implementation plan (Markdown) handed to Codex
    "review": "TEXT",  # Claude's structured review of Codex's changes (JSON)
    "claude_usage": "TEXT",  # accumulated Claude tokens and estimated cost for this task (JSON)
    "phase": "TEXT",  # step within coding: planning, coding, reviewing or fixing
    "decisions": "TEXT",  # human answers to Codex's questions, oldest first (JSON list)
    "declined_approvals": "TEXT",  # approvals declined because nobody answered in time (JSON list)
    "allowed_approvals": "TEXT",  # declined approvals a human allowed for later runs (JSON list)
}
MAX_EVENT_CHARS = 8000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(message: str) -> str:
    """Keep the start and the end of long output; the tail usually holds the failure."""
    if len(message) <= MAX_EVENT_CHARS:
        return message
    marker = "\n… [truncated] …\n"
    head = 2000
    return message[:head] + marker + message[-(MAX_EVENT_CHARS - head - len(marker)):]


class Database:
    def __init__(self, path: Path, *, max_events_per_task: int = 500) -> None:
        self.path = path
        self.max_events_per_task = max_events_per_task

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
            # auto_vacuum only takes effect after a VACUUM; do it once so pruning shrinks the file.
            if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                db.execute("VACUUM")
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
            for name, definition in MIGRATED_COLUMNS.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')  # names are constants

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
            db.execute("UPDATE tasks SET state='interrupted', error='Daemon stopped before completion', updated_at=? WHERE state='coding'", (_now(),))

    def append_event(self, issue_key: str, kind: str, message: str) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO task_events(issue_key,timestamp,kind,message) VALUES (?,?,?,?)',
                       (issue_key, _now(), kind, _bounded(message)))
            db.execute('DELETE FROM task_events WHERE issue_key=? AND id NOT IN (SELECT id FROM task_events WHERE issue_key=? ORDER BY id DESC LIMIT ?)',
                       (issue_key, issue_key, self.max_events_per_task))

    def events(self, issue_key: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_events WHERE issue_key=? AND id>? ORDER BY id LIMIT 200', (issue_key, after))]

    def request_resume(self, issue_key: str) -> None:
        with self.connect() as db:
            cursor = db.execute("UPDATE tasks SET state='resume_pending', attempts=0, next_attempt_at=NULL WHERE issue_key=? AND state IN ('interrupted','failed','needs_human')", (issue_key,))
            if cursor.rowcount != 1:
                raise ValueError('Task must be interrupted, failed or waiting for you to resume')

    def upsert_task(self, issue_key: str, summary: str, state: TaskState = TaskState.QUEUED) -> None:
        now = _now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO tasks(issue_key, summary, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(issue_key) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at""",
                (issue_key, summary, state.value, now, now),
            )

    def update_task(self, issue_key: str, state: TaskState, **fields: object) -> None:
        allowed = {"worktree", "session_id", "result", "error", "progress", "attempts", "next_attempt_at",
                   "report", "retry_note", "prompt", "blocked_reason", "plan", "review",
                   "claude_usage", "phase", "decisions", "declined_approvals", "allowed_approvals"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported fields: {sorted(invalid)}")
        values = {key: json.dumps(value) if key in ("result", "report", "review", "claude_usage", "decisions", "declined_approvals",
                                     "allowed_approvals") and value is not None else value
                  for key, value in fields.items()}
        values.update(state=state.value, updated_at=_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE tasks SET {assignments} WHERE issue_key = ?",  # keys are allow-listed
                (*values.values(), issue_key),
            )

    def get_task(self, issue_key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE issue_key=?", (issue_key,)).fetchone()
            return dict(row) if row else None

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

    def due_recovery(self) -> list[dict]:
        """Interrupted or resume-requested tasks whose retry back-off has elapsed, oldest first."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM tasks WHERE state IN ('resume_pending','interrupted')
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at""", (_now(),))]

    def reconcile_queue(self, issues: Iterable[tuple[str, str] | dict]) -> None:
        """Archive waiting issues outside a complete JQL snapshot; preserve work history.

        Queued and blocked issues follow Jira (a blocker finishing re-queues the issue);
        every other state belongs to the agent or a human and is left alone.
        """
        now = _now()
        items = [issue if isinstance(issue, dict) else {"key": issue[0], "summary": issue[1]} for issue in issues]
        keys = {item["key"] for item in items}
        with self.connect() as db:
            for row in db.execute("SELECT issue_key FROM tasks WHERE state IN ('queued','blocked')").fetchall():
                if row[0] not in keys:
                    db.execute("UPDATE tasks SET state='excluded', updated_at=? WHERE issue_key=?", (now, row[0]))
            for item in items:
                blocked = item.get("blocked_reason") or None
                db.execute(
                    """INSERT INTO tasks(issue_key,summary,state,description,issue_type,priority,blocked_reason,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(issue_key) DO UPDATE SET
                    summary=excluded.summary,
                    updated_at=CASE WHEN tasks.state IN ('excluded','queued','blocked') THEN excluded.updated_at ELSE tasks.updated_at END,
                    description=COALESCE(excluded.description, tasks.description),
                    issue_type=COALESCE(excluded.issue_type, tasks.issue_type),
                    priority=COALESCE(excluded.priority, tasks.priority),
                    blocked_reason=excluded.blocked_reason,
                    state=CASE WHEN tasks.state IN ('excluded','queued','blocked') THEN excluded.state ELSE tasks.state END""",
                    (item["key"], item["summary"], "blocked" if blocked else "queued", item.get("description"),
                     item.get("issue_type"), item.get("priority"), blocked, now, now),
                )

    def reset_task(self, issue_key: str, state: TaskState, *, retry_note: str | None = None) -> None:
        """Forget a task's work (worktree, session, result, logs) but keep the Jira data."""
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE tasks SET state=?, worktree=NULL, session_id=NULL, result=NULL, error=NULL,
                   progress=NULL, report=NULL, prompt=NULL, plan=NULL, review=NULL, claude_usage=NULL, phase=NULL, decisions=NULL,
                   declined_approvals=NULL, allowed_approvals=NULL, attempts=0, next_attempt_at=NULL,
                   retry_note=?, updated_at=? WHERE issue_key=?""",
                (state.value, retry_note, _now(), issue_key))
            if cursor.rowcount != 1:
                raise ValueError('Task not found')
            db.execute("DELETE FROM task_events WHERE issue_key=?", (issue_key,))

    def delete_task(self, issue_key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM task_events WHERE issue_key=?", (issue_key,))
            db.execute("DELETE FROM tasks WHERE issue_key=?", (issue_key,))

    def clear_events(self, issue_key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM task_events WHERE issue_key=?", (issue_key,))

    def prune(self, retention_days: int) -> int:
        """Drop history older than the retention window; returns removed event count.

        Logs of finished work (done/skipped/excluded) expire; excluded rows expire entirely.
        Skipped rows stay so the agent keeps remembering not to redo them.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self.connect() as db:
            removed = db.execute(
                """DELETE FROM task_events WHERE issue_key IN (SELECT issue_key FROM tasks
                   WHERE state IN ('done','skipped','excluded') AND updated_at < ?)
                   OR issue_key NOT IN (SELECT issue_key FROM tasks)""", (cutoff,)).rowcount
            removed += db.execute(
                """DELETE FROM task_events WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER
                   (PARTITION BY issue_key ORDER BY id DESC) AS newest FROM task_events) WHERE newest > ?)""",
                (self.max_events_per_task,)).rowcount
            db.execute("DELETE FROM tasks WHERE state='excluded' AND updated_at < ?", (cutoff,))
            db.execute("UPDATE tasks SET progress=NULL, prompt=NULL WHERE state IN ('done','skipped') AND updated_at < ?", (cutoff,))
        with self.connect() as db:
            db.execute("PRAGMA incremental_vacuum")
        return removed

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
