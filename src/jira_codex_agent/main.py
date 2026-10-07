from __future__ import annotations

import asyncio
import json
import logging
import fcntl
from logging.handlers import RotatingFileHandler
from pathlib import Path

from jira_codex_agent.codex.quota import QuotaReader
from jira_codex_agent.codex.runner import CodexRunner
from jira_codex_agent.config import Settings
from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.jira.client import JiraClient
from jira_codex_agent.scheduler.orchestrator import Orchestrator
from jira_codex_agent.storage.database import Database, TaskState


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class ControlServer:
    def __init__(self, path: Path, database: Database, orchestrator: Orchestrator | None = None) -> None:
        self.path = path
        self.database = database
        self.orchestrator = orchestrator

    async def start(self) -> asyncio.AbstractServer:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=self.path)
        self.path.chmod(0o600)
        return server

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            command = request.get("command")
            if command == "status":
                coding = self.database.list_tasks(TaskState.CODING)
                response = {"running": True, "paused": self.database.is_paused(), "current": coding[0] if coding else None}
            elif command == "tasks":
                response = {"tasks": self.database.list_tasks()}
            elif command in ('quota', 'quota_cached'):
                if not self.orchestrator:
                    raise ValueError('Quota reader unavailable')
                quota = self.orchestrator.quota
                if command == 'quota':
                    snapshot = await quota.read(force=True)
                    self.orchestrator.wake_event.set()
                else:
                    snapshot = quota.cached
                response = {'snapshot': snapshot.model_dump(mode='json') if snapshot else None,
                            'checkedAt': quota.checked_at.isoformat() if quota.checked_at else None,
                            'fiveHourThreshold': self.orchestrator.settings.quota_remaining_threshold,
                            'weeklyThreshold': self.orchestrator.settings.quota_weekly_remaining_threshold}
            elif command == 'approvals':
                runner = self.orchestrator.runner if self.orchestrator else None
                response = {'approvals': list(runner.approvals.values()) if runner else []}
            elif command.startswith('approval:'):
                if not self.orchestrator:
                    raise ValueError('Approval handling unavailable')
                _, token, decision = command.split(':', 2)
                self.orchestrator.runner.answer_approval(token, decision)
                response = {'paused': self.database.is_paused()}
            elif command.startswith('logs:'):
                _, key, cursor = command.split(':', 2)
                response = {'events': self.database.events(key, max(0, int(cursor)))}
            elif command == 'fetch_jira':
                if not self.orchestrator:
                    raise ValueError('Jira synchronization unavailable')
                issues = await self.orchestrator.sync_jira()
                response = {'count': len(issues)}
            elif command == 'stop':
                if not self.orchestrator:
                    raise ValueError('Daemon stop unavailable')
                asyncio.get_running_loop().call_later(0.2, self.orchestrator.stop)
                response = {'paused': self.database.is_paused()}
            elif command in {"pause", "resume"}:
                self.database.set_paused(command == "pause")
                response = {"paused": self.database.is_paused()}
                if self.orchestrator:
                    self.orchestrator.wake_event.set()
            elif command.startswith('resume_task:'):
                self.database.request_resume(command.split(':', 1)[1])
                if self.orchestrator:
                    self.orchestrator.wake_event.set()
                response = {'accepted': True, 'paused': self.database.is_paused()}
            elif command == "review":
                response = {"tasks": self.database.list_tasks(TaskState.REVIEW)}
            elif command.startswith(('review_done:', 'delete_worktree:')):
                action, key = command.split(':', 1)
                task = next((row for row in self.database.list_tasks() if row['issue_key'] == key), None)
                if not task:
                    raise ValueError('Task not found')
                if action == 'review_done':
                    if task['state'] != 'review':
                        raise ValueError('Task is not awaiting review')
                    self.database.update_task(key, TaskState.DONE)
                else:
                    if task['state'] not in ('failed', 'done'):
                        raise ValueError('Only failed or reviewed tasks can be cleaned up')
                    if not self.orchestrator or not task.get('worktree'):
                        raise ValueError('No worktree to remove')
                    await self.orchestrator.worktrees.remove(key, Path(task['worktree']))
                    self.database.update_task(key, TaskState(task['state']), worktree=None)
                response = {'paused': self.database.is_paused()}
            else:
                response = {"error": f"unknown command: {command}"}
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except Exception as exc:
            writer.write((json.dumps({"error": str(exc)}) + "\n").encode())
        finally:
            writer.close()
            await writer.wait_closed()


async def run_daemon(settings: Settings | None = None) -> None:
    settings = settings or Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    daemon_lock = (settings.data_dir / 'daemon.lock').open('a')
    try:
        fcntl.flock(daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        daemon_lock.close()
        raise RuntimeError('Another daemon already owns this data directory') from None
    database = Database(settings.database_path)
    database.initialize()
    database.recover_interrupted()
    control = ControlServer(settings.socket_path, database)
    server = await control.start()
    jira = JiraClient(settings.jira_url, settings.jira_email, settings.jira_api_token.get_secret_value())
    orchestrator = Orchestrator(
        settings,
        database,
        jira,
        CodexRunner(settings.codex_command, settings.codex_model, settings.max_task_seconds,
                    approvals_reviewer=settings.codex_approvals_reviewer),
        QuotaReader(settings.codex_command),
        WorktreeManager(settings.repository, settings.worktree_root, settings.base_branch),
    )
    control.orchestrator = orchestrator
    try:
        async with server:
            await orchestrator.run_forever()
    finally:
        await jira.close()
        settings.socket_path.unlink(missing_ok=True)
        daemon_lock.close()


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
