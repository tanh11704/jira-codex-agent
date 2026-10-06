from __future__ import annotations

import asyncio
import logging
import json
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
        self.wake_event = asyncio.Event()
        self.jira_sync_lock = asyncio.Lock()

    async def sync_jira(self) -> list[JiraIssue]:
        async with self.jira_sync_lock:
            issues = await self.jira.search(self.settings.jira_jql)
            self.database.reconcile_queue([(issue.key, issue.summary) for issue in issues])
            return issues

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
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
                await asyncio.wait_for(self.wake_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            self.wake_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    async def run_cycle(self) -> bool:
        pending = self.database.list_tasks(TaskState.RESUME_PENDING)
        if pending:
            if self.settings.dry_run or not await self._respect_quota():
                return False
            if self.database.is_paused() or self.stop_event.is_set():
                return False
            issue = await self.jira.get_issue(pending[0]['issue_key'])
            await self.process(issue, resume=pending[0])
            return True
        issues = await self.sync_jira()
        queued = self.database.list_tasks(TaskState.QUEUED)
        if not queued:
            return False
        if self.settings.dry_run:
            log.info("dry run: would process %s", queued[0]["issue_key"])
            return False
        if not await self._respect_quota():
            return False
        if self.database.is_paused() or self.stop_event.is_set():
            return False
        queued_keys = {row["issue_key"] for row in queued}
        issue = next((item for item in issues if item.key in queued_keys), None)
        if issue is None:
            return False
        await self.process(issue)
        return True

    async def process(self, issue: JiraIssue, resume: dict | None = None) -> None:
        try:
            if resume:
                from pathlib import Path
                if not resume.get('worktree') or not Path(resume['worktree']).is_dir():
                    raise RuntimeError('Original worktree missing; restore it before resuming')
                worktree = Path(resume['worktree'])
            else:
                worktree = await self.worktrees.create(issue.key)
            self.database.update_task(issue.key, TaskState.CODING, worktree=str(worktree), error=None)
            def record(event: dict) -> None:
                fields = {'progress': json.dumps(event, ensure_ascii=False)[-16000:]}
                if event.get('thread_id'):
                    fields['session_id'] = event['thread_id']
                self.database.update_task(issue.key, TaskState.CODING, **fields)

            prompt = self._prompt(issue)
            if resume:
                prompt = 'Continue the interrupted task in this existing worktree. Inspect and preserve existing changes, finish remaining work and run tests.\n' + prompt
            run = asyncio.create_task(self.runner.run(prompt, worktree, session_id=resume.get('session_id') if resume else None, on_event=record))
            stopped = asyncio.create_task(self.stop_event.wait())
            try:
                done, _ = await asyncio.wait([run, stopped], return_when=asyncio.FIRST_COMPLETED)
                if run not in done:
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)
                    self.database.update_task(issue.key, TaskState.INTERRUPTED, error='Daemon stopped; resume to continue')
                    return
                result = await run
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
            state = TaskState.REVIEW if result.outcome == RunOutcome.SUCCEEDED else (TaskState.INTERRUPTED if result.outcome == RunOutcome.TIMED_OUT else TaskState.FAILED)
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

    async def _respect_quota(self) -> bool:
        while not self.stop_event.is_set():
            snapshot = await self.quota.read()
            windows = [window for window in (snapshot.primary, snapshot.secondary) if window]
            if snapshot.error or not windows:
                log.warning("quota unavailable; deferring task: %s", snapshot.error or "no quota windows")
                return False
            blocked = [window for window in windows if window.remaining_percent <= (
                self.settings.quota_weekly_remaining_threshold
                if (window.window_minutes or 0) > 300
                else self.settings.quota_remaining_threshold
            )]
            if not blocked and snapshot.ordinary_usage_allowed is not False:
                return True
            if not blocked:
                log.warning("backend denies usage; deferring task")
                return False
            if any(window.resets_at is None for window in blocked):
                log.warning("blocked quota has no reset timestamp; deferring task")
                return False
            reset = max(window.resets_at for window in blocked if window.resets_at)
            seconds = (reset - datetime.now(timezone.utc)).total_seconds()
            if seconds <= 0:
                log.warning("quota reset passed but usage still blocked; deferring task")
                return False
            log.info("quota blocked (%s); sleeping %.0fs", [(w.window_minutes, w.remaining_percent) for w in blocked], seconds)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=seconds + 5)
            except TimeoutError:
                continue
        return False

    @staticmethod
    def _prompt(issue: JiraIssue) -> str:
        description = adf_to_text(issue.fields.description).strip() or "No description supplied."
        return f"""Implement Jira issue {issue.key}: {issue.summary}

Description:
{description}

Work autonomously in this repository. Inspect existing conventions first, implement the smallest complete solution, and run relevant tests. Do not push, open a pull request, or change Jira. Leave all changes in the current worktree and finish with a concise summary plus any human-review notes.
"""
