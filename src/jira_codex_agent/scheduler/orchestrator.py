from __future__ import annotations

import asyncio
import logging
import json
import signal
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jira_codex_agent.codex.models import CodexRunResult, RunOutcome
from jira_codex_agent.codex.quota import QuotaReader
from jira_codex_agent.codex.report import REPORT_SCHEMA, output_contract, parse_report
from jira_codex_agent.codex.runner import CodexRunner
from jira_codex_agent.config import Settings
from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.jira.client import JiraClient
from jira_codex_agent.jira.models import JiraIssue, adf_to_text
from jira_codex_agent.macos import SleepGuard
from jira_codex_agent.storage.database import Database, TaskState

log = logging.getLogger(__name__)

# Keep the Mac awake through quota waits up to this long; longer waits let it sleep.
MAX_AWAKE_QUOTA_WAIT = 6 * 3600
PRUNE_INTERVAL = 3600


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        jira: JiraClient,
        runner: CodexRunner,
        quota: QuotaReader,
        worktrees: WorktreeManager,
        *,
        notify=None,
        sleep_guard: SleepGuard | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.jira = jira
        self.runner = runner
        self.quota = quota
        self.worktrees = worktrees
        self.notify = notify or (lambda title, message: None)
        self.sleep_guard = sleep_guard or SleepGuard(enabled=False)
        self.stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.jira_sync_lock = asyncio.Lock()
        self.current_key: str | None = None
        self.abort_event = asyncio.Event()
        self.idle_event = asyncio.Event()
        self.idle_event.set()
        self._last_prune: float | None = None

    def _setting(self, name: str, default):
        return getattr(self.settings, name, default)

    async def sync_jira(self) -> list[JiraIssue]:
        async with self.jira_sync_lock:
            issues = await self.jira.search(self.settings.jira_jql)
            flag_field = await self.jira.flag_field() if hasattr(self.jira, 'flag_field') else None
            self.database.reconcile_queue([self._queue_item(issue, flag_field) for issue in issues])
            return issues

    @staticmethod
    def _queue_item(issue: JiraIssue, flag_field: str | None) -> dict:
        item = {'key': issue.key, 'summary': issue.summary}
        fields = getattr(issue, 'fields', None)
        if fields is not None:
            issue_type, priority = getattr(fields, 'issuetype', None), getattr(fields, 'priority', None)
            item.update(description=adf_to_text(fields.description).strip() or None,
                        issue_type=issue_type.name if issue_type else None,
                        priority=priority.name if priority else None)
        if hasattr(issue, 'blockers'):
            item['blocked_reason'] = '; '.join(issue.blockers(flag_field)) or None
        return item

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
                self._maintenance()  # retention applies even while paused
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

    def _maintenance(self) -> None:
        if self._last_prune is not None and time.monotonic() - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = time.monotonic()
        try:
            removed = self.database.prune(self._setting('history_retention_days', 30))
            if removed:
                log.info("pruned %d old log events", removed)
        except Exception:
            log.exception("history pruning failed")

    async def run_cycle(self) -> bool:
        pending = self.database.due_recovery()
        if pending:
            if self.settings.dry_run or not await self._respect_quota():
                return False
            if self.database.is_paused() or self.stop_event.is_set():
                return False
            task = pending[0]
            try:
                issue = await self.jira.get_issue(task['issue_key'])
            except Exception as exc:
                # A deleted or unreadable issue must not block the whole queue forever.
                self._attempt_failed(task['issue_key'], f'Could not load Jira issue: {exc}')
                return True
            await self.process(issue, resume=task)
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
        # A recovery request may arrive while fetching Jira or waiting on quota.
        # Re-select recovery work on the next cycle instead of starting a new issue.
        if self.database.due_recovery():
            return True
        queued_keys = {row["issue_key"] for row in queued}
        issue = next((item for item in issues if item.key in queued_keys), None)
        if issue is None:
            return False
        await self.process(issue)
        return True

    async def process(self, issue: JiraIssue, resume: dict | None = None) -> None:
        self.current_key = issue.key
        self.abort_event.clear()
        self.idle_event.clear()
        try:
            with self.sleep_guard.hold():
                await self._process(issue, resume)
        except Exception as exc:
            log.exception("task %s failed", issue.key)
            self.database.append_event(issue.key, 'error', str(exc))
            self._attempt_failed(issue.key, str(exc))
        finally:
            self.current_key = None
            self.idle_event.set()

    async def _process(self, issue: JiraIssue, resume: dict | None) -> None:
        task = self.database.get_task(issue.key) or {}
        session_id = resume.get('session_id') if resume else None
        if resume and not (resume.get('worktree') and Path(resume['worktree']).is_dir()):
            self.database.append_event(issue.key, 'lifecycle', 'Original worktree missing; starting again in a fresh worktree')
            resume, session_id = None, None
        worktree = Path(resume['worktree']) if resume else await self.worktrees.create(issue.key)
        prompt = self._prompt(issue, task)
        if resume:
            prompt = 'Continue the interrupted task in this existing worktree. Inspect and preserve existing changes, finish remaining work and run tests.\n' + prompt
        self.database.update_task(issue.key, TaskState.CODING, worktree=str(worktree), error=None,
                                  next_attempt_at=None, prompt=prompt)
        self.database.append_event(issue.key, 'lifecycle', 'Resuming Codex session' if resume else 'Starting Codex task')

        def record(event: dict) -> None:
            fields = {'progress': json.dumps(event, ensure_ascii=False)[-16000:]}
            if event.get('thread_id'):
                fields['session_id'] = event['thread_id']
            self.database.update_task(issue.key, TaskState.CODING, **fields)
            item = event.get('item', {})
            message = item.get('text') or event.get('message') or ''
            if item.get('command'):
                message = str(item['command']) + '\n' + str(item.get('aggregated_output', ''))
                if item.get('exit_code') is not None:
                    message += f"\nExit code: {item['exit_code']}"
            if not message:
                message = json.dumps(event, ensure_ascii=False)
            self.database.append_event(issue.key, event.get('type', 'event'), str(message))

        run = asyncio.create_task(self.runner.run(prompt, worktree, session_id=session_id, on_event=record,
                                                  output_schema=REPORT_SCHEMA))
        stopped = asyncio.create_task(self.stop_event.wait())
        aborted = asyncio.create_task(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait([run, stopped, aborted], return_when=asyncio.FIRST_COMPLETED)
            if run not in done:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                if aborted in done:
                    # The discard request that set abort_event decides the final state.
                    self.database.append_event(issue.key, 'lifecycle', 'Aborted by user')
                    return
                self.database.update_task(issue.key, TaskState.INTERRUPTED, error='Daemon stopped; resume to continue')
                self.database.append_event(issue.key, 'lifecycle', 'Interrupted: daemon stopped')
                return
            result = await run
        finally:
            for waiter in (stopped, aborted):
                waiter.cancel()
            await asyncio.gather(stopped, aborted, return_exceptions=True)
        if self._finish(issue.key, result) == TaskState.REVIEW:
            await self._respect_quota()

    def _finish(self, key: str, result: CodexRunResult) -> TaskState:
        if result.outcome != RunOutcome.SUCCEEDED:
            self.database.update_task(key, TaskState.CODING, session_id=result.session_id,
                                      result=result.model_dump(mode="json"))
            self._attempt_failed(key, result.error or result.outcome.value)
            return TaskState.INTERRUPTED
        report = parse_report(result.final_message)
        status = report['status'] if report else None
        if status == 'COMPLETED':
            state, error = TaskState.REVIEW, None
        else:
            # Codex finished but could not deliver; a human decides, the queue moves on.
            state = TaskState.NEEDS_HUMAN
            blockers = '; '.join(report.get('blockers') or []) if report else ''
            error = (status or 'No status reported') + (f': {blockers}' if blockers else '')
        summary = (report or {}).get('summary') or result.final_message
        self.database.append_event(key, 'lifecycle', f'Task {state.value}: {error or summary}')
        self.database.update_task(key, state, session_id=result.session_id, result=result.model_dump(mode="json"),
                                  report=report, error=error, attempts=0, next_attempt_at=None)
        self.notify(f'{key}: ' + ('ready for review' if state == TaskState.REVIEW else 'needs you'),
                    error or summary or 'Codex finished')
        return state

    def _attempt_failed(self, key: str, error: str) -> None:
        """Retry transient failures with linear back-off, then give up as failed."""
        task = self.database.get_task(key) or {}
        attempts = (task.get('attempts') or 0) + 1
        limit = self._setting('max_task_attempts', 3)
        if attempts < limit:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=self._setting('retry_backoff_seconds', 600) * attempts)
            message = f'{error} (attempt {attempts}/{limit}; retrying after {retry_at.astimezone():%H:%M})'
            self.database.update_task(key, TaskState.INTERRUPTED, attempts=attempts,
                                      next_attempt_at=retry_at.isoformat(), error=message)
            self.database.append_event(key, 'lifecycle', 'Will retry: ' + message)
        else:
            message = f'{error} (gave up after {attempts} attempts)'
            self.database.update_task(key, TaskState.FAILED, attempts=attempts, next_attempt_at=None, error=message)
            self.database.append_event(key, 'lifecycle', 'Failed: ' + message)
            self.notify(f'{key}: failed', message)

    async def discard(self, key: str, mode: str, note: str | None = None) -> list[str]:
        """Throw away a task's work. retry → queue again from scratch, skip → never redo, delete → forget."""
        if mode not in ('retry', 'skip', 'delete'):
            raise ValueError('Unknown discard mode')
        task = self.database.get_task(key)
        if not task:
            raise ValueError('Task not found')
        if self.current_key == key and task['state'] == TaskState.CODING:
            self.abort_event.set()
            await asyncio.wait_for(self.idle_event.wait(), 30)
            task = self.database.get_task(key) or task
        deleted = await self.worktrees.discard(key, Path(task['worktree']) if task.get('worktree') else None)
        if task.get('session_id') and hasattr(self.runner, 'archive_thread'):
            try:
                await self.runner.archive_thread(task['session_id'])
            except Exception as exc:
                log.warning("could not archive Codex thread %s: %s", task['session_id'], exc)
        if mode == 'delete':
            self.database.delete_task(key)
        else:
            self.database.reset_task(key, TaskState.QUEUED if mode == 'retry' else TaskState.SKIPPED,
                                     retry_note=(note or None) if mode == 'retry' else None)
        self.wake_event.set()
        return deleted

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
            # Without the assertion an idle Mac sleeps and the monotonic timer stalls past the reset.
            with self.sleep_guard.hold() if seconds <= MAX_AWAKE_QUOTA_WAIT else nullcontext():
                try:
                    await asyncio.wait_for(self.wake_event.wait(), timeout=seconds + 5)
                    self.wake_event.clear()
                except TimeoutError:
                    continue
        return False

    def _prompt(self, issue: JiraIssue, task: dict | None = None) -> str:
        description = adf_to_text(issue.fields.description).strip() or "No description supplied."
        context = ''
        links = []
        for link in getattr(issue.fields, 'issuelinks', None) or []:
            kind = link.get('type') or {}
            direction, other = ('inward', link['inwardIssue']) if 'inwardIssue' in link else ('outward', link.get('outwardIssue'))
            if other:
                status = ((other.get('fields') or {}).get('status') or {}).get('name', 'unknown')
                links.append(f"- {kind.get(direction, 'relates to')} {other.get('key')}: "
                             f"{(other.get('fields') or {}).get('summary', '')} [{status}]")
        if links:
            context += '\nLinked issues:\n' + '\n'.join(links) + '\n'
        if task and task.get('attempts') and task.get('error'):
            context += f"\nThe previous attempt did not finish: {task['error']}\n"
        if task and task.get('retry_note'):
            context += f"\nA human discarded an earlier attempt and asks:\n{task['retry_note']}\n"
        return f"""Implement Jira issue {issue.key}: {issue.summary}

Description:
{description}
{context}
{self.settings.task_instructions}

{output_contract(self._setting('approval_timeout_seconds', 0))}
"""
