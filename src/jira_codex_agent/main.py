from __future__ import annotations

import asyncio
import json
import logging
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
    def __init__(self, path: Path, database: Database) -> None:
        self.path = path
        self.database = database

    async def start(self) -> asyncio.AbstractServer:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        return await asyncio.start_unix_server(self._handle, path=self.path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            command = request.get("command")
            if command == "status":
                coding = self.database.list_tasks(TaskState.CODING)
                response = {"running": True, "paused": self.database.is_paused(), "current": coding[0] if coding else None}
            elif command == "tasks":
                response = {"tasks": self.database.list_tasks()}
            elif command in {"pause", "resume"}:
                self.database.set_paused(command == "pause")
                response = {"paused": self.database.is_paused()}
            elif command == "review":
                response = {"tasks": self.database.list_tasks(TaskState.REVIEW)}
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
    database = Database(settings.database_path)
    database.initialize()
    control = ControlServer(settings.socket_path, database)
    server = await control.start()
    jira = JiraClient(settings.jira_url, settings.jira_email, settings.jira_api_token.get_secret_value())
    orchestrator = Orchestrator(
        settings,
        database,
        jira,
        CodexRunner(settings.codex_command, settings.codex_model, settings.max_task_seconds),
        QuotaReader(settings.codex_command),
        WorktreeManager(settings.repository, settings.worktree_root, settings.base_branch),
    )
    try:
        async with server:
            await orchestrator.run_forever()
    finally:
        await jira.close()
        settings.socket_path.unlink(missing_ok=True)


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
