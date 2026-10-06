from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from jira_codex_agent.codex.models import RunOutcome
from jira_codex_agent.codex.quota import QuotaReader
from jira_codex_agent.codex.runner import CodexRunner
from jira_codex_agent.config import Settings
from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.jira.client import JiraClient
from jira_codex_agent.jira.models import JiraIssue, adf_to_text
from jira_codex_agent.storage.database import Database, TaskState

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        jira: JiraClient,
        runner: CodexRunner,
        quota: QuotaReader,
        worktrees: WorktreeManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.jira = jira
        self.runner = runner
        self.quota = quota
        self.worktrees = worktrees
        self.stop_event = asyncio.Event()

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        log.info("orchestrator started")
        while not self.stop_event.is_set():
            delay = self.settings.jira_poll_seconds
            try:
                if self.database.is_paused():
                    log.info("agent paused")
                else:
                    processed = await self.run_cycle()
                    delay = 1 if processed else self.settings.jira_poll_seconds
            except Exception:
                log.exception("scheduler cycle failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def run_cycle(self) -> bool:
        issues = await self.jira.search(self.settings.jira_jql)
        known = self.database.task_keys()
        for issue in issues:
            if issue.key not in known:
                self.database.upsert_task(issue.key, issue.summary)
        queued = self.database.list_tasks(TaskState.QUEUED)
        if not queued:
            return False
        if self.settings.dry_run:
            log.info("dry run: would process %s", queued[0]["issue_key"])
            return False
        issue = next((item for item in issues if item.key == queued[0]["issue_key"]), None)
        if issue is None:
            issue = await self.jira.get_issue(queued[0]["issue_key"])
        await self.process(issue)
        return True

    async def process(self, issue: JiraIssue) -> None:
        try:
            worktree = await self.worktrees.create(issue.key)
            self.database.update_task(issue.key, TaskState.CODING, worktree=str(worktree), error=None)
            result = await self.runner.run(self._prompt(issue), worktree)
            state = TaskState.REVIEW if result.outcome == RunOutcome.SUCCEEDED else TaskState.FAILED
            self.database.update_task(
                issue.key,
                state,
                session_id=result.session_id,
                result=result.model_dump(mode="json"),
                error=result.error,
            )
            if state == TaskState.REVIEW:
                await self._respect_quota()
        except Exception as exc:
            self.database.update_task(issue.key, TaskState.FAILED, error=str(exc))
            raise

    async def _respect_quota(self) -> None:
        snapshot = await self.quota.read()
        window = snapshot.five_hour_window()
        if snapshot.error:
            log.warning("quota unavailable: %s", snapshot.error)
            return
        should_wait = snapshot.ordinary_usage_allowed is False or (
            window is not None and window.remaining_percent <= self.settings.quota_remaining_threshold
        )
        if not should_wait or not window or not window.resets_at:
            return
        seconds = max(0, (window.resets_at - datetime.now(timezone.utc)).total_seconds())
        log.info("quota at %s%% remaining; sleeping %.0fs", window.remaining_percent, seconds)
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds + 5)
        except TimeoutError:
            pass

    @staticmethod
    def _prompt(issue: JiraIssue) -> str:
        description = adf_to_text(issue.fields.description).strip() or "No description supplied."
        return f"""Implement Jira issue {issue.key}: {issue.summary}

Description:
{description}

Work autonomously in this repository. Inspect existing conventions first, implement the smallest complete solution, and run relevant tests. Do not push, open a pull request, or change Jira. Leave all changes in the current worktree and finish with a concise summary plus any human-review notes.
"""
