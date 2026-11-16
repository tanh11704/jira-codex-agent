from __future__ import annotations

import asyncio
import logging
import json
import signal
import subprocess
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jira_codex_agent.claude.prompts import (BACKEND_SCOPE, CLASSIFY_SCHEMA, PLAN_SCHEMA, REVIEW_SCHEMA, classify_prompt,
                                             findings_text, fix_prompt, frontend_scope, merge_reports, plan_markdown,
                                             plan_prompt, plan_section, review_prompt)
from jira_codex_agent.claude.quota import ClaudeQuotaReader
from jira_codex_agent.claude.runner import ClaudeResult, ClaudeRunner
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
from jira_codex_agent.verify.checks import check_fix_prompt, checks_section, failure_feedback, run_checks

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
        claude: ClaudeRunner | None = None,
        claude_quota: ClaudeQuotaReader | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.jira = jira
        self.runner = runner
        self.quota = quota
        self.worktrees = worktrees
        self.claude = claude
        self.claude_quota = claude_quota
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
        self.database.update_task(issue.key, TaskState.CODING, worktree=str(worktree), error=None, next_attempt_at=None)
        # A resumed run keeps the plan and routing it started with; worktree changes would skew new ones.
        plan = task.get('plan') if resume else None
        area = task.get('area') if resume else None
        claude_codes_ui = bool(self.claude and self._setting('claude_frontend', False))
        if not resume and self.claude and self._setting('claude_plan', False):
            self.database.update_task(issue.key, TaskState.CODING, phase='planning')
            finished, planned = await self._guarded(issue.key, self._plan(issue, task, worktree))
            if not finished:
                return
            plan, area = planned
        if not resume and claude_codes_ui and area is None:
            self.database.update_task(issue.key, TaskState.CODING, phase='planning')
            finished, area = await self._guarded(issue.key, self._classify(issue, task, worktree))
            if not finished:
                return

        if claude_codes_ui and (task.get('phase') == 'frontend' if resume else area == 'frontend'):
            backend = json.loads(task['backend_report']) if resume and task.get('backend_report') else None
            finished, result = await self._guarded(issue.key, self._code_frontend(
                issue, task, worktree, plan, backend, resume=bool(resume), codex_session=session_id))
            if not finished:
                return
            if result is not None:
                self._finish(issue.key, result)
                return
            if resume:
                # Resumed frontend work waits for Claude rather than restarting it in a Codex session that never saw it.
                self._attempt_failed(issue.key, 'Claude Code unavailable to continue the frontend work')
                return
            area = None  # Claude cannot code now; Codex implements the whole issue.

        split = claude_codes_ui and area == 'fullstack'
        scope = BACKEND_SCOPE if split else ''
        prompt = self._prompt(issue, task, plan, scope)
        if resume:
            prompt = self._resume_preamble(task) + prompt
        self.database.update_task(issue.key, TaskState.CODING, prompt=prompt, phase='coding')
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
            if event.get('type') == 'approval.timeout' and event.get('approval'):
                # Keep what was declined so a human can allow it and resume later.
                approval = {k: event['approval'].get(k) for k in ('kind', 'command', 'cwd', 'reason')}
                declined = self._json(self.database.get_task(issue.key), 'declined_approvals')
                if approval not in declined:
                    self.database.update_task(issue.key, TaskState.CODING, declined_approvals=declined + [approval])

        codex_fix = self._codex_fixer(task, worktree, record)

        async def code() -> CodexRunResult:
            first = await self.runner.run(prompt, worktree, session_id=session_id, on_event=record,
                                          output_schema=REPORT_SCHEMA, preapproved=self._json(task, 'allowed_approvals'))
            return await self._verify(issue.key, worktree, first, codex_fix, 'Codex')

        finished, result = await self._guarded(issue.key, code())
        if not finished:
            return
        review, review_skipped = None, None
        if self.claude and self._setting('claude_review', False):
            finished, reviewed = await self._guarded(issue.key, self._review_loop(issue, task, worktree, plan, result, record,
                                                                                  scope, codex_fix))
            if not finished:
                return
            result, review, review_skipped = reviewed
        backend = parse_report(result.final_message) if split and result.outcome == RunOutcome.SUCCEEDED else None
        if backend and backend['status'] == 'COMPLETED' and not (review and review.get('verdict') == 'REQUEST_CHANGES'):
            finished, frontend = await self._guarded(issue.key, self._frontend_after_backend(
                issue, task, worktree, plan, backend, result, codex_fix))
            if not finished:
                return
            result = frontend
        if self._finish(issue.key, result, review, review_skipped) == TaskState.REVIEW:
            await self._respect_quota()

    def _codex_fixer(self, task: dict, worktree: Path, record):
        """A follow-up turn in the same Codex thread, used for check failures and review findings."""
        async def fix(prompt: str, previous: CodexRunResult) -> CodexRunResult:
            return await self.runner.run(prompt, worktree, session_id=previous.session_id, on_event=record,
                                         output_schema=REPORT_SCHEMA, preapproved=self._json(task, 'allowed_approvals'))
        return fix

    def _verify_commands(self) -> list[str]:
        return list(getattr(self.settings, 'verify_commands', None) or [])

    async def _verify(self, key: str, worktree: Path, result: CodexRunResult, fix, agent: str) -> CodexRunResult:
        """Run the configured build/test commands ourselves after a COMPLETED report.

        A failure goes back to the agent as a trimmed excerpt for a bounded number of rounds; what still fails
        downgrades the report to PARTIAL so a human looks at it. Passing checks are added to the report."""
        commands = self._verify_commands()
        rounds = self._setting('verify_fix_rounds', 2)
        timeout = self._setting('verify_timeout_seconds', 1800)
        for attempt in range(rounds + 1):
            report = parse_report(result.final_message) if commands and result.outcome == RunOutcome.SUCCEEDED else None
            if not report or report['status'] != 'COMPLETED':
                return result
            self.database.update_task(key, TaskState.CODING, phase='verifying')
            self.database.append_event(key, 'lifecycle', f'Running {len(commands)} local check(s)')
            results = await run_checks(commands, worktree, timeout, on_result=lambda item: self.database.append_event(
                key, 'check', f"{item.command} → {'PASS' if item.ok else 'FAIL'} ({item.seconds}s)"
                + ('' if item.ok else '\n' + item.output)))
            checked = {item.command: 'PASS' if item.ok else 'FAIL' for item in results}
            report['tests'] = [test for test in report.get('tests') or [] if test.get('command') not in checked] + [
                {'command': command, 'result': outcome} for command, outcome in checked.items()]
            if all(item.ok for item in results):
                return result.model_copy(update={'final_message': json.dumps(report, ensure_ascii=False)})
            failed = results[-1].command
            if attempt == rounds:
                self.database.append_event(key, 'lifecycle', f'Local check still failing: {failed}')
                report.update(status='PARTIAL', blockers=(report.get('blockers') or []) + [f'Local check failed: {failed}'])
                return result.model_copy(update={'final_message': json.dumps(report, ensure_ascii=False)})
            self.database.update_task(key, TaskState.CODING, phase='fixing')
            self.database.append_event(key, 'lifecycle', f'{agent} fixing failed check {failed} (round {attempt + 1}/{rounds})')
            feedback = failure_feedback(results, commands[len(results):])
            result = await fix(check_fix_prompt(feedback, output_contract(self._setting('approval_timeout_seconds', 0))), result)
        return result

    @staticmethod
    def _json(task: dict | None, column: str) -> list:
        return json.loads((task or {}).get(column) or '[]')

    def _resume_preamble(self, task: dict, approvals: bool = True) -> str:
        """Tell the resumed Codex session what changed since it stopped."""
        parts = ['Continue the interrupted task in this existing worktree. Inspect and preserve existing changes, '
                 'finish remaining work and run tests.']
        report = json.loads(task['report']) if task.get('report') else None
        if report and report.get('status') != 'COMPLETED':
            blockers = '; '.join(report.get('blockers') or [])
            parts.append(f"Your previous run ended {report['status']}" + (f': {blockers}' if blockers else '.'))
        if self._json(task, 'decisions'):
            parts.append('A human has now answered your questions; see HUMAN DECISIONS below and continue accordingly.')
        review = json.loads(task['review']) if task.get('review') else None
        if report and report.get('status') == 'COMPLETED' and review and review.get('verdict') == 'REQUEST_CHANGES':
            parts.append('A code reviewer still requests changes; fix every valid blocker and major finding.\n'
                         + findings_text(review))
        if approvals and (allowed := self._json(task, 'allowed_approvals')):
            parts.append('A human allowed these previously declined requests. Request them again when needed; '
                         'they will be approved:\n' + '\n'.join(f"- {item.get('command') or item.get('kind')}" for item in allowed))
        return '\n'.join(parts) + '\n'

    def answer(self, key: str, answers: list[dict], note: str | None, allow: list[int]) -> None:
        """Record a human's decisions and resume the same Codex session with them."""
        task = self.database.get_task(key)
        if not task or task['state'] not in ('needs_human', 'failed', 'interrupted'):
            raise ValueError('Task is not waiting for you')
        answered = [{'question': str(item.get('question', '')), 'answer': str(item['answer']).strip()}
                    for item in answers if str(item.get('answer') or '').strip()]
        note = (note or '').strip() or None
        declined = self._json(task, 'declined_approvals')
        allowed = [declined[index] for index in sorted(set(allow)) if 0 <= index < len(declined)]
        if not answered and not note and not allowed:
            raise ValueError('Answer at least one question, add a note or allow a request')
        fields: dict = {'declined_approvals': [item for item in declined if item not in allowed] or None}
        if answered or note:
            fields['decisions'] = self._json(task, 'decisions') + [
                {'answered_at': datetime.now(timezone.utc).isoformat(), 'answers': answered, 'note': note}]
        if allowed:
            fields['allowed_approvals'] = self._json(task, 'allowed_approvals') + allowed
        self.database.update_task(key, TaskState(task['state']), **fields)
        self.database.append_event(key, 'lifecycle', f'Human answered {len(answered)} question(s)'
                                   + (f', allowed {len(allowed)} request(s)' if allowed else '') + '; resuming')
        self.database.request_resume(key)
        self.wake_event.set()

    async def _guarded(self, key: str, work) -> tuple[bool, object]:
        """Await work unless the daemon stops or the user aborts first; returns (finished, result)."""
        run = asyncio.ensure_future(work)
        stopped = asyncio.create_task(self.stop_event.wait())
        aborted = asyncio.create_task(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait([run, stopped, aborted], return_when=asyncio.FIRST_COMPLETED)
            if run not in done:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                if aborted in done:
                    # The discard request that set abort_event decides the final state.
                    self.database.append_event(key, 'lifecycle', 'Aborted by user')
                else:
                    self.database.update_task(key, TaskState.INTERRUPTED, error='Daemon stopped; resume to continue')
                    self.database.append_event(key, 'lifecycle', 'Interrupted: daemon stopped')
                return False, None
            return True, await run
        finally:
            for waiter in (stopped, aborted):
                waiter.cancel()
            await asyncio.gather(stopped, aborted, return_exceptions=True)

    async def _ask_claude(self, key: str, prompt: str, worktree: Path, schema: dict, *, check_quota: bool = True,
                          **options) -> ClaudeResult:
        """Run one Claude pass when the subscription has quota to spare and record the tokens it used."""
        if check_quota and (blocked := await self._claude_quota_blocked()):
            return ClaudeResult(ok=False, error=blocked)
        outcome = await self.claude.run(prompt, worktree, schema=schema,
                                        on_event=lambda event: self.database.append_event(key, event['type'], event['message']),
                                        **options)
        usage = json.loads((self.database.get_task(key) or {}).get('claude_usage') or '{}')
        usage = {'input_tokens': usage.get('input_tokens', 0) + outcome.input_tokens,
                 'output_tokens': usage.get('output_tokens', 0) + outcome.output_tokens,
                 'passes': usage.get('passes', 0) + 1}
        self.database.update_task(key, TaskState.CODING, claude_usage=usage)
        self.database.append_event(key, 'claude.usage',
                                   f'{outcome.input_tokens:,} input / {outcome.output_tokens:,} output tokens')
        return outcome

    async def _claude_quota_blocked(self) -> str | None:
        """Why Claude sits this pass out to keep the subscription's reserve; None when it may run."""
        if not self.claude_quota:
            return None
        snapshot = await self.claude_quota.read()
        windows = [window for window in (snapshot.primary, snapshot.secondary) if window]
        if snapshot.error or not windows:
            return f'Claude usage unavailable: {snapshot.error or "no limits reported"}'
        for window in windows:
            weekly = (window.window_minutes or 0) > 300
            threshold = self._setting('claude_quota_weekly_remaining_threshold' if weekly
                                      else 'claude_quota_remaining_threshold', 30)
            if window.remaining_percent <= threshold:
                reset = f', resets {window.resets_at.astimezone():%a %H:%M}' if window.resets_at else ''
                return (f"Claude {'weekly' if weekly else '5-hour'} quota at {window.remaining_percent}% remaining "
                        f"(stop at {threshold}%){reset}")
        return None

    async def _plan(self, issue: JiraIssue, task: dict, worktree: Path) -> tuple[str | None, str | None]:
        """Ask Claude for an implementation plan and the issue's area; Codex plans alone when Claude is unavailable."""
        self.database.append_event(issue.key, 'lifecycle', 'Claude planning')
        outcome = await self._ask_claude(issue.key, plan_prompt(self._task_text(issue, task), self.settings.task_instructions),
                                         worktree, PLAN_SCHEMA)
        if not outcome.ok:
            self.database.append_event(issue.key, 'lifecycle', f'Claude plan unavailable; Codex plans alone: {outcome.error}')
            return None, None
        plan, area = plan_markdown(outcome.data), outcome.data.get('area')
        self.database.update_task(issue.key, TaskState.CODING, plan=plan, area=area)
        self.database.append_event(issue.key, 'lifecycle', f"Claude plan ready ({area or 'area unknown'}): "
                                   + outcome.data.get('summary', ''))
        return plan, area

    async def _classify(self, issue: JiraIssue, task: dict, worktree: Path) -> str | None:
        """Ask Claude whether the issue is frontend, backend or full-stack work; None leaves it all to Codex."""
        self.database.append_event(issue.key, 'lifecycle', 'Claude classifying the issue')
        outcome = await self._ask_claude(issue.key, classify_prompt(self._task_text(issue, task)), worktree, CLASSIFY_SCHEMA)
        if not outcome.ok:
            self.database.append_event(issue.key, 'lifecycle', f'Could not classify the issue; Codex codes it: {outcome.error}')
            return None
        area = outcome.data.get('area')
        self.database.update_task(issue.key, TaskState.CODING, area=area)
        self.database.append_event(issue.key, 'lifecycle', f"Issue classified as {area}: {outcome.data.get('reason', '')}")
        return area

    async def _code_frontend(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                             backend: dict | None, *, resume: bool, codex_session: str | None) -> CodexRunResult | None:
        """Claude Code implements the UI in the worktree; None when its quota is reserved and it cannot run now.

        The result is shaped like a Codex run so the same finishing rules apply; Codex's thread id is kept."""
        key = issue.key
        if blocked := await self._claude_quota_blocked():
            self.database.append_event(key, 'lifecycle', f'Claude Code cannot code the frontend now: {blocked}')
            return None
        previous = task.get('claude_session_id') if resume else None
        self.database.update_task(key, TaskState.CODING, phase='frontend')
        self.database.append_event(key, 'lifecycle', 'Claude Code resuming the frontend work' if previous
                                   else 'Claude Code implementing the frontend')
        prompt = self._prompt(issue, task, plan, frontend_scope(backend))
        if resume:
            prompt = self._resume_preamble(task, approvals=False) + prompt
        options = dict(write=True, timeout=self._setting('max_task_seconds', 7200))

        async def fresh() -> ClaudeResult:
            session = str(uuid.uuid4())
            self.database.update_task(key, TaskState.CODING, claude_session_id=session)
            return await self._ask_claude(key, prompt, worktree, REPORT_SCHEMA, check_quota=False, session_id=session, **options)

        outcome = None
        if previous:
            outcome = await self._ask_claude(key, prompt, worktree, REPORT_SCHEMA, check_quota=False, resume=previous, **options)
            if not outcome.ok and not outcome.session_id:
                # The CLI never got going (e.g. the saved session is gone); start a session that reads the worktree.
                self.database.append_event(key, 'lifecycle', f'Could not resume the Claude session ({outcome.error}); starting a new one')
                outcome = None
        outcome = outcome or await fresh()

        def shaped(claude: ClaudeResult) -> CodexRunResult:
            if not claude.ok:
                return CodexRunResult(outcome=RunOutcome.FAILED, session_id=codex_session, error=f'Claude Code: {claude.error}')
            return CodexRunResult(outcome=RunOutcome.SUCCEEDED, session_id=codex_session,
                                  final_message=json.dumps(claude.data, ensure_ascii=False))

        async def fix(prompt: str, _previous: CodexRunResult) -> CodexRunResult:
            session = (self.database.get_task(key) or {}).get('claude_session_id')
            return shaped(await self._ask_claude(key, prompt, worktree, REPORT_SCHEMA, check_quota=False,
                                                 resume=session, **options))

        result = await self._verify(key, worktree, shaped(outcome), fix, 'Claude Code')
        report = parse_report(result.final_message) if backend and result.outcome == RunOutcome.SUCCEEDED else None
        if report:
            result = result.model_copy(update={'final_message': json.dumps(merge_reports(backend, report), ensure_ascii=False)})
        return result

    async def _frontend_after_backend(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                                      backend: dict, result: CodexRunResult, codex_fix) -> CodexRunResult:
        """Second half of a full-stack issue: Claude codes the UI, or Codex does when Claude cannot run."""
        self.database.update_task(issue.key, TaskState.CODING, backend_report=backend, session_id=result.session_id)
        frontend = await self._code_frontend(issue, task, worktree, plan, backend, resume=False,
                                             codex_session=result.session_id)
        if frontend is not None:
            return frontend
        self.database.update_task(issue.key, TaskState.CODING, phase='coding')
        self.database.append_event(issue.key, 'lifecycle', 'Codex implementing the frontend instead')
        frontend = await codex_fix(
            'The backend part is done. Now implement the frontend part of the issue in this worktree on top of it, '
            'following the same rules and plan, recapture screenshots of the changed UI and report on the whole issue.\n\n'
            + output_contract(self._setting('approval_timeout_seconds', 0)), result)
        return await self._verify(issue.key, worktree, frontend, codex_fix, 'Codex')

    async def _review_loop(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                           result: CodexRunResult, record, scope: str = '',
                           codex_fix=None) -> tuple[CodexRunResult, dict | None, str | None]:
        """Claude reviews COMPLETED work; Codex fixes requested changes for a bounded number of rounds.

        Returns the final Codex result, the last review and, when the latest change went unreviewed, why."""
        rounds, review, skipped = self._setting('claude_review_rounds', 1), None, None
        for attempt in range(rounds + 1):
            report = parse_report(result.final_message) if result.outcome == RunOutcome.SUCCEEDED else None
            if not report or report['status'] != 'COMPLETED':
                break
            self.database.update_task(issue.key, TaskState.CODING, phase='reviewing')
            self.database.append_event(issue.key, 'lifecycle', 'Claude reviewing changes')
            outcome = await self._ask_claude(issue.key, review_prompt(self._task_text(issue, task, scope), plan, report),
                                             worktree, REVIEW_SCHEMA)
            if not outcome.ok:
                # Unreviewed work goes to the human queue rather than looking as vetted as reviewed work.
                self.database.append_event(issue.key, 'lifecycle', f'Claude review unavailable: {outcome.error}')
                skipped = outcome.error or 'Claude review unavailable'
                if review is not None:
                    # An earlier review describes code Codex has since changed; keep it only in the log.
                    self.database.update_task(issue.key, TaskState.CODING, review=None)
                review = None
                break
            review = outcome.data
            self.database.update_task(issue.key, TaskState.CODING, review=review)
            self.database.append_event(issue.key, 'lifecycle', f"Claude review: {review['verdict']} — {review.get('summary', '')}")
            if review['verdict'] != 'REQUEST_CHANGES' or attempt == rounds:
                break
            self.database.update_task(issue.key, TaskState.CODING, phase='fixing')
            self.database.append_event(issue.key, 'lifecycle', f'Codex fixing review findings (round {attempt + 1}/{rounds})')
            codex_fix = codex_fix or self._codex_fixer(task, worktree, record)
            result = await codex_fix(fix_prompt(review, output_contract(self._setting('approval_timeout_seconds', 0))), result)
            result = await self._verify(issue.key, worktree, result, codex_fix, 'Codex')
        return result, review, skipped

    def _finish(self, key: str, result: CodexRunResult, review: dict | None = None,
                review_skipped: str | None = None) -> TaskState:
        if result.outcome != RunOutcome.SUCCEEDED:
            self.database.update_task(key, TaskState.CODING, session_id=result.session_id,
                                      result=result.model_dump(mode="json"))
            self._attempt_failed(key, result.error or result.outcome.value)
            return TaskState.INTERRUPTED
        report = parse_report(result.final_message)
        if report and report.get('images'):
            report = self._check_images(key, report)
        status = report['status'] if report else None
        if status == 'COMPLETED' and review and review.get('verdict') == 'REQUEST_CHANGES':
            # Codex says done but Claude still sees blocking issues after the allowed fix rounds.
            state = TaskState.NEEDS_HUMAN
            error = 'Claude review requested changes: ' + (review.get('summary') or 'see review findings')
        elif status == 'COMPLETED' and review_skipped:
            # Review is enabled but did not run; check the change (and its screenshots) by hand.
            state = TaskState.NEEDS_HUMAN
            error = f'Claude review skipped ({review_skipped}); review the change and screenshots manually'
        elif status == 'COMPLETED':
            state, error = TaskState.REVIEW, None
        else:
            # Codex finished but could not deliver; a human decides, the queue moves on.
            state = TaskState.NEEDS_HUMAN
            blockers = '; '.join(report.get('blockers') or []) if report else ''
            questions = len((report or {}).get('questions') or [])
            if questions:
                blockers = f"{questions} question{'s' if questions > 1 else ''} to answer" + (f'; {blockers}' if blockers else '')
            error = (status or 'No status reported') + (f': {blockers}' if blockers else '')
        summary = (report or {}).get('summary') or result.final_message
        self.database.append_event(key, 'lifecycle', f'Task {state.value}: {error or summary}')
        self.database.update_task(key, state, session_id=result.session_id, result=result.model_dump(mode="json"),
                                  report=report, error=error, attempts=0, next_attempt_at=None,
                                  **({'review': review} if review is not None else {}))
        self.notify(f'{key}: ' + ('ready for review' if state == TaskState.REVIEW else 'needs you'),
                    error or summary or 'Codex finished')
        return state

    def _check_images(self, key: str, report: dict) -> dict:
        """Mark each reported image slot whose file is not in the worktree, whatever source the agent claimed.

        resolved_path is absolute so the UI can re-check it live once the human saves a generated image there."""
        worktree = (self.database.get_task(key) or {}).get('worktree')
        if not worktree:
            return report
        root = Path(worktree).resolve()
        files: list[str] | None = None
        images, missing = [], []
        for image in report['images']:
            relative = str(image.get('path') or '').strip().lstrip('/')
            target = (root / relative).resolve() if relative else None
            if target is not None and not target.is_relative_to(root):
                target = None  # never look outside the worktree
            if target is not None and not target.is_file():
                # Agents sometimes give the path relative to a portal (public/images/...); match it as a suffix.
                if files is None:
                    files = self._worktree_files(root)
                matches = [name for name in files if name == relative or name.endswith('/' + relative)]
                if len(matches) == 1:
                    target = root / matches[0]
            present = bool(target and target.is_file())
            images.append({**image, 'resolved_path': str(target) if target else '', 'missing': not present})
            if not present:
                missing.append(image.get('placement') or relative or '(no path)')
        if missing:
            self.database.append_event(key, 'lifecycle', f'{len(missing)} image file(s) missing: ' + '; '.join(missing))
        return {**report, 'images': images}

    @staticmethod
    def _worktree_files(root: Path) -> list[str]:
        try:
            output = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=root,
                                    capture_output=True, text=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return output.splitlines()

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

    def _task_text(self, issue: JiraIssue, task: dict | None = None, scope: str = '') -> str:
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
        if decisions := self._json(task, 'decisions'):
            lines = []
            for decision in decisions:
                lines += [f"- Q: {item['question']}\n  A: {item['answer']}" for item in decision.get('answers') or []]
                if decision.get('note'):
                    lines.append(f"- Note: {decision['note']}")
            context += '\nHUMAN DECISIONS (authoritative answers to earlier questions; follow them):\n' + '\n'.join(lines) + '\n'
        return f"""Implement Jira issue {issue.key}: {issue.summary}

Description:
{description}
{context}{scope}"""

    def _prompt(self, issue: JiraIssue, task: dict | None = None, plan: str | None = None, scope: str = '') -> str:
        checks = checks_section(commands) if (commands := self._verify_commands()) else ''
        return f"""{self._task_text(issue, task, scope)}{plan_section(plan) if plan else ''}
{self.settings.task_instructions}
{checks}
{output_contract(self._setting('approval_timeout_seconds', 0))}
"""
