from __future__ import annotations

import asyncio
import logging
import json
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jira_codex_agent.agents import AGENT_NAMES, CODER_NAMES, DEFAULT_AGENTS
from jira_codex_agent.antigravity.runner import AntigravityRunner, unavailable
from jira_codex_agent.claude.prompts import (BACKEND_SCOPE, CLASSIFY_SCHEMA, DECIDE_SCHEMA, IMPLEMENTER_SCOPE, PLAN_SCHEMA,
                                             REVIEW_SCHEMA, RISKS, classify_prompt, decide_prompt, decision_lines, decisions_prompt,
                                             decisions_report,
                                             findings_text, fix_prompt, frontend_scope, merge_reports, plan_markdown,
                                             plan_prompt, plan_section, review_prompt)
from jira_codex_agent.claude.quota import ClaudeQuotaReader
from jira_codex_agent.claude.runner import ClaudeResult, ClaudeRunner
from jira_codex_agent.codex.models import CodexRunResult, RunOutcome
from jira_codex_agent.codex.quota import QuotaReader
from jira_codex_agent.codex.report import REPORT_SCHEMA, output_contract, parse_report
from jira_codex_agent.codex.runner import CodexRunner
from jira_codex_agent.config import DEFAULT_DECIDE_INSTRUCTIONS, Settings
from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.jira.client import JiraClient
from jira_codex_agent.jira.models import JiraIssue, adf_to_text
from jira_codex_agent.macos import SleepGuard
from jira_codex_agent.storage.database import Database, TaskState
from jira_codex_agent.verify.checks import (CheckResult, check_fix_prompt, checks_section, failure_feedback,
                                            failure_signature, killed_by_timeout, run_check, timed_out)
from jira_codex_agent.verify.images import changed_image_references, is_declared
from jira_codex_agent.verify.ui_capture import CaptureResult, capture as capture_ui, capture_section

log = logging.getLogger(__name__)

# Keep the Mac awake through quota waits up to this long; longer waits let it sleep.
MAX_AWAKE_QUOTA_WAIT = 6 * 3600
PRUNE_INTERVAL = 3600
# Which agent's settings describe a pass; classification is part of planning.
PASS_ROLES = {'classify': 'plan'}


@dataclass
class Coder:
    """An agent that writes code in the worktree. start() returns None when the agent cannot run now."""
    agent: str
    roles: tuple[str, ...]  # jobs credited to it: backend, frontend or both
    start: Callable[[str], Awaitable[CodexRunResult | None]]
    fix: Callable[[str, CodexRunResult], Awaitable[CodexRunResult]]

    @property
    def name(self) -> str:
        return CODER_NAMES[self.agent]


# How the daemon's screenshot and layout pass appears in a report's tests.
CAPTURE_COMMAND = 'UI capture (automation)'
ISSUE_KEY = re.compile(r'\b[A-Z][A-Z0-9]+-\d+\b')

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
        antigravity: AntigravityRunner | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.jira = jira
        self.runner = runner
        self.quota = quota
        self.worktrees = worktrees
        self.claude = claude
        self.claude_quota = claude_quota
        self.antigravity = antigravity
        self.notify = notify or (lambda title, message: None)
        self.sleep_guard = sleep_guard or SleepGuard(enabled=False)
        self.stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.jira_sync_lock = asyncio.Lock()
        self._last_unblock_check: float | None = None
        self.current_key: str | None = None
        self.abort_event = asyncio.Event()
        self.idle_event = asyncio.Event()
        self.idle_event.set()
        self._last_prune: float | None = None
        self._deciding: set[str] = set()
        self._baseline: dict[str, dict[str, dict]] = {}  # base commit → command → {ok, signature}
        self._background: set[asyncio.Task] = set()

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
        self._decide_backlog()
        await self._resume_unblocked()
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
        recheck = bool(resume and task.get('recheck'))
        if recheck:
            self.database.update_task(issue.key, TaskState(task['state']), recheck=0)  # one recheck per request
        if resume and not (resume.get('worktree') and Path(resume['worktree']).is_dir()):
            self.database.append_event(issue.key, 'lifecycle', 'Original worktree missing; starting again in a fresh worktree')
            resume, session_id = None, None
        worktree = Path(resume['worktree']) if resume else await self.worktrees.create(issue.key)
        self.database.update_task(issue.key, TaskState.CODING, worktree=str(worktree), error=None, next_attempt_at=None)
        # A resumed run keeps the plan and routing it started with; worktree changes would skew new ones.
        plan = task.get('plan') if resume else None
        area = task.get('area') if resume else None
        split = self._splits_frontend()
        if not resume and self._setting('claude_plan', False) and self._available('plan'):
            self.database.update_task(issue.key, TaskState.CODING, phase='planning')
            finished, planned = await self._guarded(issue.key, self._plan(issue, task, worktree))
            if not finished:
                return
            plan, area, questions = planned
            if questions and self._deciding_enabled():
                finished, _ = await self._guarded(issue.key, self._decide_plan(issue, task, worktree, plan, questions))
                if not finished:
                    return
                task = self.database.get_task(issue.key) or task
        if not resume and split and area is None and self._available('plan'):
            self.database.update_task(issue.key, TaskState.CODING, phase='planning')
            finished, area = await self._guarded(issue.key, self._classify(issue, task, worktree))
            if not finished:
                return

        if split and (task.get('phase') == 'frontend' if resume else area == 'frontend'):
            backend = json.loads(task['backend_report']) if resume and task.get('backend_report') else None
            finished, result = await self._guarded(issue.key, self._code_frontend(
                issue, task, worktree, plan, backend, resume=bool(resume),
                codex_session=session_id, backend_agent=self._agent_for(task, 'backend'), recheck=recheck))
            if not finished:
                return
            if result is not None:
                self._finish(issue.key, result)
                return
            if resume:
                # Resumed frontend work waits for its agent rather than restarting it with one that never saw it.
                self._attempt_failed(issue.key, f"{CODER_NAMES[self._agent_for(task, 'frontend')]} unavailable "
                                                'to continue the frontend work')
                return
            split = False  # The frontend agent cannot code now; the backend agent implements the whole issue.

        fullstack = split and area == 'fullstack'
        scope = BACKEND_SCOPE if fullstack else ''
        roles = ('backend',) if fullstack or area == 'backend' or area is None else (
            ('frontend',) if area == 'frontend' else ('backend', 'frontend'))
        coder = await self._backend_coder(issue, task, worktree, resume=bool(resume), codex_session=session_id,
                                          roles=roles)
        prompt = self._prompt(issue, task, plan, scope + ('' if coder.agent == 'codex' else IMPLEMENTER_SCOPE))
        if resume:
            prompt = self._resume_preamble(task, approvals=coder.agent == 'codex') + prompt
        self.database.update_task(issue.key, TaskState.CODING, prompt=prompt, phase='coding')
        self.database.append_event(issue.key, 'lifecycle', f'Resuming {coder.name} session' if resume
                                   else f'Starting {coder.name} task')

        async def code() -> CodexRunResult:
            nonlocal coder
            first = self._recheck_result(issue.key, task, session_id) if recheck else await coder.start(prompt)
            if first is None:
                self.database.append_event(issue.key, 'lifecycle', f'{coder.name} unavailable; Codex implements it instead')
                coder = self._codex_coder(issue.key, task, worktree, session_id, roles)
                first = await coder.start(self._prompt(issue, task, plan, scope))
            verified = await self._verify(issue.key, worktree, first, coder.fix, coder.name)
            return await self._settle(issue, worktree, plan, verified, coder.fix, coder.name, scope)

        finished, result = await self._guarded(issue.key, code())
        if not finished:
            return
        review, review_skipped = None, None
        if self._setting('claude_review', False) and self._available('review'):
            finished, reviewed = await self._guarded(issue.key, self._review_loop(issue, task, worktree, plan, result,
                                                                                  coder, scope))
            if not finished:
                return
            result, review, review_skipped = reviewed
        backend = parse_report(result.final_message) if fullstack and result.outcome == RunOutcome.SUCCEEDED else None
        if backend and backend['status'] == 'COMPLETED' and not (review and review.get('verdict') == 'REQUEST_CHANGES'):
            finished, frontend = await self._guarded(issue.key, self._frontend_after_backend(
                issue, task, worktree, plan, backend, result, coder))
            if not finished:
                return
            result = frontend
        if self._finish(issue.key, result, review, review_skipped) == TaskState.REVIEW:
            await self._respect_quota()

    # Agents and roles

    def _role(self, role: str) -> dict:
        """The agent, model and effort configured for a job; empty model/effort fall back to the agent's defaults."""
        role = PASS_ROLES.get(role, role)
        agent = self._setting(f'{role}_agent', None) or DEFAULT_AGENTS[role]
        if agent == 'codex':
            return {'agent': 'codex', 'model': self._setting('codex_model', None),
                    'effort': self._setting('codex_reasoning_effort', None)}
        model = self._setting(f'{role}_model', None) or self._setting(f'{agent}_model', None)
        effort = self._setting(f'{role}_effort', None) or self._setting(f'{agent}_effort', None)
        return {'agent': agent, 'model': model or None, 'effort': effort or None}

    def _runner_for(self, agent: str):
        return {'claude': self.claude, 'antigravity': self.antigravity}.get(agent)

    def _available(self, role: str) -> bool:
        agent = self._role(role)['agent']
        return agent == 'codex' or self._runner_for(agent) is not None

    def _splits_frontend(self) -> bool:
        """Frontend work goes to its own agent when routing is on and that agent differs from the backend one."""
        return (bool(self._setting('claude_frontend', False)) and self._available('frontend')
                and self._role('frontend')['agent'] != self._role('backend')['agent'])

    def _agent_for(self, task: dict, role: str) -> str:
        """The agent that already holds this job for the task, so resumed work continues where it started."""
        return (self._json_object(task, 'agents').get(role) or {}).get('agent') or self._role(role)['agent']

    @staticmethod
    def _json_object(task: dict | None, column: str) -> dict:
        return json.loads((task or {}).get(column) or '{}')

    def _credit(self, key: str, roles: tuple[str, ...] | list[str], agent: str, model: str | None,
                effort: str | None, outcome: ClaudeResult | None = None, *, passes: int = 1) -> None:
        """Record who does a job, so the review shows e.g. PLAN: Claude Opus, FE: Antigravity Gemini."""
        current = self.database.get_task(key) or {}
        agents = self._json_object(current, 'agents')
        for role in roles:
            entry = agents.get(role) or {}
            if entry.get('agent') != agent:
                entry = {}
            agents[role] = {'agent': agent, 'model': (outcome.model if outcome else None) or model or entry.get('model'),
                            'effort': effort or entry.get('effort'),
                            'input_tokens': entry.get('input_tokens', 0) + (outcome.input_tokens if outcome else 0),
                            'output_tokens': entry.get('output_tokens', 0) + (outcome.output_tokens if outcome else 0),
                            'passes': entry.get('passes', 0) + passes}
        self.database.update_task(key, TaskState(current.get('state') or TaskState.CODING.value), agents=agents)

    def _sessions(self, key: str) -> dict:
        task = self.database.get_task(key) or {}
        sessions = self._json_object(task, 'agent_sessions')
        if task.get('claude_session_id'):
            sessions.setdefault('frontend', task['claude_session_id'])  # tasks started before sessions were per role
        return sessions

    def _remember_session(self, key: str, role: str, session: str) -> None:
        task = self.database.get_task(key) or {}
        sessions = {**self._json_object(task, 'agent_sessions'), role: session}
        self.database.update_task(key, TaskState(task.get('state') or TaskState.CODING.value), agent_sessions=sessions)

    def _codex_recorder(self, key: str):
        def record(event: dict) -> None:
            fields = {'progress': json.dumps(event, ensure_ascii=False)[-16000:]}
            if event.get('thread_id'):
                fields['session_id'] = event['thread_id']
            self.database.update_task(key, TaskState.CODING, **fields)
            item = event.get('item', {})
            message = item.get('text') or event.get('message') or ''
            if item.get('command'):
                message = str(item['command']) + '\n' + str(item.get('aggregated_output', ''))
                if item.get('exit_code') is not None:
                    message += f"\nExit code: {item['exit_code']}"
            if not message:
                message = json.dumps(event, ensure_ascii=False)
            self.database.append_event(key, event.get('type', 'event'), str(message))
            if event.get('type') == 'approval.timeout' and event.get('approval'):
                # Keep what was declined so a human can allow it and resume later.
                approval = {k: event['approval'].get(k) for k in ('kind', 'command', 'cwd', 'reason')}
                declined = self._json(self.database.get_task(key), 'declined_approvals')
                if approval not in declined:
                    self.database.update_task(key, TaskState.CODING, declined_approvals=declined + [approval])
        return record

    def _codex_coder(self, key: str, task: dict, worktree: Path, session_id: str | None,
                     roles: tuple[str, ...]) -> Coder:
        record = self._codex_recorder(key)
        spec = {'model': self._setting('codex_model', None), 'effort': self._setting('codex_reasoning_effort', None)}
        coder = Coder('codex', roles, None, None)  # type: ignore[arg-type]

        def credited(result: CodexRunResult) -> CodexRunResult:
            if result.outcome == RunOutcome.SUCCEEDED:
                self._credit(key, coder.roles, 'codex', spec['model'], spec['effort'])
            return result

        async def start(prompt: str) -> CodexRunResult:
            self._credit(key, coder.roles, 'codex', spec['model'], spec['effort'], passes=0)
            return credited(await self.runner.run(prompt, worktree, session_id=session_id, on_event=record,
                                                  output_schema=REPORT_SCHEMA,
                                                  preapproved=self._json(task, 'allowed_approvals')))

        async def fix(prompt: str, previous: CodexRunResult) -> CodexRunResult:
            """A follow-up turn in the same Codex thread, used for check failures, decisions and review findings."""
            return credited(await self.runner.run(prompt, worktree, session_id=previous.session_id, on_event=record,
                                                  output_schema=REPORT_SCHEMA,
                                                  preapproved=self._json(task, 'allowed_approvals')))

        coder.start, coder.fix = start, fix
        return coder

    def _cli_coder(self, key: str, agent: str, role: str, worktree: Path, *, resume: bool,
                   codex_session: str | None, roles: tuple[str, ...]) -> Coder:
        """Claude Code or Antigravity coding in the worktree; results are shaped like a Codex run so the same
        finishing rules apply, and Codex's thread id is kept."""
        name = CODER_NAMES[agent]
        spec = self._role(role) if self._role(role)['agent'] == agent else {'model': None, 'effort': None}
        options = dict(write=True, timeout=self._setting('max_task_seconds', 7200),
                       on_session=lambda session: self._remember_session(key, role, session))
        coder = Coder(agent, roles, None, None)  # type: ignore[arg-type]

        def shaped(outcome: ClaudeResult) -> CodexRunResult:
            if not outcome.ok:
                return CodexRunResult(outcome=RunOutcome.FAILED, session_id=codex_session, error=f'{name}: {outcome.error}')
            return CodexRunResult(outcome=RunOutcome.SUCCEEDED, session_id=codex_session,
                                  final_message=json.dumps(outcome.data, ensure_ascii=False))

        async def ask(prompt: str, **extra) -> ClaudeResult:
            return await self._ask(role, key, prompt, worktree, REPORT_SCHEMA, check_quota=False, agent=agent,
                                   credit=coder.roles, **options, **extra)

        async def fresh(prompt: str) -> ClaudeResult:
            if agent == 'claude':
                session = str(uuid.uuid4())  # Claude takes a preset id, so an interrupted run can be resumed
                self._remember_session(key, role, session)
                return await ask(prompt, session_id=session)
            return await ask(prompt)  # agy names the conversation; on_session stores it as soon as it starts

        async def start(prompt: str) -> CodexRunResult | None:
            self._credit(key, coder.roles, agent, spec['model'], spec['effort'], passes=0)
            outcome = None
            if resume and (previous := self._sessions(key).get(role)):
                outcome = await ask(prompt, resume=previous)
                if not outcome.ok and not outcome.session_id:
                    # The CLI never got going (e.g. the saved session is gone); start a session that reads the worktree.
                    self.database.append_event(key, 'lifecycle', f'Could not resume the {name} session '
                                               f'({outcome.error}); starting a new one')
                    outcome = None
            if outcome is None:
                outcome = await fresh(prompt)
                if not outcome.ok and agent == 'antigravity' and unavailable(outcome.error):
                    self.database.append_event(key, 'lifecycle', f'{name} cannot code now: {outcome.error}')
                    return None
            return shaped(outcome)

        async def fix(prompt: str, _previous: CodexRunResult) -> CodexRunResult:
            return shaped(await ask(prompt, resume=self._sessions(key).get(role)))

        coder.start, coder.fix = start, fix
        return coder

    async def _coder(self, role: str, key: str, task: dict, worktree: Path, *, resume: bool,
                     codex_session: str | None, roles: tuple[str, ...]) -> Coder | None:
        """The agent that codes this part, or None when it cannot run now (missing CLI, Claude quota reserved)."""
        agent = self._agent_for(task, role) if resume else self._role(role)['agent']
        if agent == 'codex':
            # Codex coding the UI after another agent's backend starts its own thread; resumed work continues it.
            fresh_thread = role == 'frontend' and not resume
            return self._codex_coder(key, task, worktree, None if fresh_thread else codex_session, roles)
        if self._runner_for(agent) is None:
            return None
        if agent == 'claude' and (blocked := await self._claude_quota_blocked()):
            self.database.append_event(key, 'lifecycle', f'Claude Code cannot code now: {blocked}')
            return None
        return self._cli_coder(key, agent, role, worktree, resume=resume, codex_session=codex_session, roles=roles)

    async def _backend_coder(self, issue: JiraIssue, task: dict, worktree: Path, *, resume: bool,
                             codex_session: str | None, roles: tuple[str, ...]) -> Coder:
        """The backend agent, or Codex when that agent cannot run now; a resumed Codex run stays with Codex."""
        coder = await self._coder('backend', issue.key, task, worktree, resume=resume, codex_session=codex_session,
                                  roles=roles)
        if coder is not None:
            return coder
        self.database.append_event(issue.key, 'lifecycle', f"{CODER_NAMES[self._role('backend')['agent']]} "
                                   'unavailable; Codex implements it instead')
        return self._codex_coder(issue.key, task, worktree, codex_session, roles)

    def _verify_commands(self) -> list[str]:
        return list(getattr(self.settings, 'verify_commands', None) or [])

    async def _verify(self, key: str, worktree: Path, result: CodexRunResult, fix, agent: str) -> CodexRunResult:
        """Run the configured build/test commands ourselves, outside the agent's sandbox, then capture the UI.

        Runs after a COMPLETED report, or a PARTIAL one whose only gap is checks the sandbox blocked. A failure goes
        back to the agent as a trimmed excerpt for a bounded number of rounds; what still fails downgrades the report
        to PARTIAL so a human looks at it. Failures that already happen on the base commit are reported, not blamed.
        Once the checks pass, the screens the agent declared in "ui_captures" are rendered and layout-checked here;
        layout problems go back like a failed check, and the screenshots go to the reviewer."""
        commands = self._verify_commands()
        rounds = self._setting('verify_fix_rounds', 2)
        timeout = self._setting('verify_timeout_seconds', 1800)
        for attempt in range(rounds + 1):
            report = parse_report(result.final_message) if result.outcome == RunOutcome.SUCCEEDED else None
            if not report or not self._verifiable(report):
                return result
            captures = (report.get('ui_captures') or []) if self._setting('ui_capture', True) else []
            if not commands and not captures:
                return result
            promoted = report['status'] != 'COMPLETED'
            self.database.update_task(key, TaskState.CODING, phase='verifying')
            results, preexisting, flaky = [], [], []
            if commands:
                self.database.append_event(key, 'lifecycle', f'Running {len(commands)} local check(s)'
                                           + (' that the sandbox blocked' if promoted else ''))
                results, preexisting, flaky = await self._run_checks(key, worktree, commands, timeout)
            checked = {item.command: 'PASS' if item.ok else ('PRE-EXISTING' if item in preexisting else 'FAIL')
                       for item in results}
            failures = [item for item in results if not item.ok and item not in preexisting]
            if not failures and captures:
                shot = await self._capture_ui(key, worktree, captures, timeout)
                report['screenshots'] = shot.screenshots
                if shot.blocked:
                    checked[CAPTURE_COMMAND] = 'BLOCKED'
                    report['report'] = ((report.get('report') or '').rstrip()
                                        + f'\n\n## Chụp UI tự động không chạy được\n{shot.blocked}\nCần kiểm tra giao diện thủ công.')
                elif shot.ok:
                    checked[CAPTURE_COMMAND] = 'PASS'
                else:
                    checked[CAPTURE_COMMAND] = 'FAIL'
                    failures = [CheckResult(command=CAPTURE_COMMAND, ok=False, exit_code=1, output=shot.output)]
            report['tests'] = [test for test in report.get('tests') or [] if test.get('command') not in checked] + [
                {'command': command, 'result': outcome} for command, outcome in checked.items()]
            if not failures:
                if promoted:
                    self.database.append_event(key, 'lifecycle', 'Checks blocked in the sandbox passed here; report upgraded to COMPLETED')
                    report.update(status='COMPLETED', blockers=[], sandbox_blocked_only=False)
                if preexisting:
                    report['report'] = (report.get('report') or '').rstrip() + '\n\n' + self._preexisting_section(preexisting)
                if flaky:
                    report['report'] = (report.get('report') or '').rstrip() + '\n\n' + self._flaky_section(flaky)
                return result.model_copy(update={'final_message': json.dumps(report, ensure_ascii=False)})
            failed = failures[-1].command
            if attempt == rounds:
                self.database.append_event(key, 'lifecycle', f'Local check still failing: {failed}')
                report.update(status='PARTIAL', blockers=(report.get('blockers') or []) + [f'Local check failed: {failed}'])
                return result.model_copy(update={'final_message': json.dumps(report, ensure_ascii=False)})
            self.database.update_task(key, TaskState.CODING, phase='fixing')
            self.database.append_event(key, 'lifecycle', f'{agent} fixing failed check {failed} (round {attempt + 1}/{rounds})')
            skipped = commands[len(results):] if failed != CAPTURE_COMMAND else []
            feedback = failure_feedback(failures, skipped)
            result = await fix(check_fix_prompt(feedback, output_contract(self._setting('approval_timeout_seconds', 0))), result)
        return result

    async def _capture_ui(self, key: str, worktree: Path, captures: list[dict], timeout: int) -> CaptureResult:
        self.database.append_event(key, 'lifecycle', f'Capturing {len(captures)} UI screen(s) outside the sandbox')
        data_dir = Path(self._setting('data_dir', None) or worktree.parent)
        shot = await capture_ui(worktree, captures, data_dir, timeout)
        outcome = 'BLOCKED' if shot.blocked else ('PASS' if shot.ok else 'FAIL')
        self.database.append_event(key, 'check', f'{CAPTURE_COMMAND} → {outcome} ({shot.seconds}s, '
                                   f'{len(shot.screenshots)} screenshot(s))'
                                   + (f'\n{shot.blocked or shot.output}' if not shot.ok else ''))
        return shot

    @staticmethod
    def _verifiable(report: dict) -> bool:
        if report['status'] == 'COMPLETED':
            return True
        if report['status'] != 'PARTIAL' or report.get('questions'):
            return False
        # A PARTIAL with nothing left for a human (only BLOCKED checks) is a sandbox gap, whatever the flag says.
        return bool(report.get('sandbox_blocked_only')) or not report.get('blockers')

    def _preexisting_section(self, failures: list[CheckResult]) -> str:
        listed = '\n'.join(f'- `{item.command}`' for item in failures)
        return ('## Check lỗi sẵn có trên base\n'
                'Các check sau cũng lỗi y hệt trên commit base, nên không tính là lỗi của task này:\n' + listed)

    def _flaky_section(self, commands: list[str]) -> str:
        listed = '\n'.join(f'- `{command}`' for command in commands)
        return ('## Check không ổn định (timeout)\n'
                'Các check sau lần đầu lỗi do timeout, chạy lại thì PASS; nên xem lại độ ổn định hoặc timeout của suite:\n'
                + listed)

    async def _run_checks(self, key: str, worktree: Path, commands: list[str],
                          timeout: int) -> tuple[list[CheckResult], list[CheckResult], list[str]]:
        """Run commands in order; stop at the first failure the base commit does not share.

        A failure that looks like a timeout is rerun first: a loaded machine makes long suites miss per-test timeouts,
        which the agent cannot fix and which would otherwise end the task PARTIAL."""
        results, preexisting, flaky = [], [], []
        retries = self._setting('verify_flaky_retries', 1)
        for command in commands:
            item = await run_check(command, worktree, timeout)
            self.database.append_event(key, 'check', f"{item.command} → {'PASS' if item.ok else 'FAIL'} ({item.seconds}s)"
                                       + ('' if item.ok else '\n' + item.output))
            for attempt in range(retries):
                killed = killed_by_timeout(item)
                if item.ok or not (timed_out(item) or killed):
                    break
                # A suite killed at the limit usually ran on a loaded machine; give the rerun twice the time.
                rerun_timeout = timeout * 2 if killed else timeout
                self.database.append_event(key, 'lifecycle', f'{command} failed with a timeout; rerunning '
                                           f'({attempt + 1}/{retries}, limit {rerun_timeout}s)')
                item = await run_check(command, worktree, rerun_timeout)
                self.database.append_event(key, 'check', f"{item.command} → {'PASS' if item.ok else 'FAIL'} "
                                           f"({item.seconds}s, rerun)" + ('' if item.ok else '\n' + item.output))
                if item.ok:
                    flaky.append(command)
            results.append(item)
            if item.ok:
                continue
            if await self._fails_on_base(key, worktree, item, timeout):
                preexisting.append(item)
                continue
            break
        return results, preexisting, flaky

    async def _fails_on_base(self, key: str, worktree: Path, failed: CheckResult, timeout: int) -> bool:
        """Whether the base commit fails this check with the same errors, so the task did not cause them."""
        if not self._setting('baseline_checks', True) or not failed.signature:
            return False
        sha = await self._base_commit(worktree)
        if not sha:
            return False
        entry = self._baseline.get(sha, {}).get(failed.command) or self._load_baseline().get(sha, {}).get(failed.command)
        if entry is None:
            self.database.append_event(key, 'lifecycle', f'Checking whether {failed.command} also fails on base {sha[:10]}')
            base = await self._run_on_base(key, sha, failed.command, timeout)
            if base is None:
                return False
            entry = {'ok': base.ok, 'signature': base.signature}
            self._remember_baseline(sha, failed.command, entry)
        if entry['ok']:
            self.database.append_event(key, 'lifecycle', f'{failed.command} passes on base {sha[:10]}; the failure comes from this change')
            return False
        new = set(failed.signature) - set(entry['signature'])
        if new:
            self.database.append_event(key, 'lifecycle', f'{failed.command} also fails on base {sha[:10]}, but '
                                       f'{len(new)} error line(s) are new, e.g. {sorted(new)[0]}')
            return False
        self.database.append_event(key, 'lifecycle', f'{failed.command} fails the same way on base {sha[:10]}; not blamed on this task')
        return True

    async def _base_commit(self, worktree: Path) -> str | None:
        base = self._setting('base_branch', 'main')
        for args in (('merge-base', 'HEAD', f'origin/{base}'), ('merge-base', 'HEAD', base), ('rev-parse', 'HEAD')):
            if sha := (await self._git_output(worktree, *args) or '').strip():
                return sha
        return None

    async def _run_on_base(self, key: str, sha: str, command: str, timeout: int) -> CheckResult | None:
        """Run a check in a shared detached worktree at the base commit; ignored files (dependencies) are kept."""
        root, repository = self._setting('worktree_root', None), self._setting('repository', None)
        if not root or not repository:
            return None
        path = Path(root) / '_baseline'
        if path.is_dir():
            ready = await self._git_output(path, 'checkout', '--detach', '--force', sha) is not None \
                and await self._git_output(path, 'clean', '-fd') is not None
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            ready = await self._git_output(Path(repository), 'worktree', 'add', '--detach', str(path), sha) is not None
        if not ready:
            self.database.append_event(key, 'lifecycle', f'Could not prepare the base worktree at {sha[:10]}')
            return None
        if setup := (self._setting('baseline_setup_command', '') or '').strip():
            prepared = await run_check(setup, path, timeout)
            if not prepared.ok:
                self.database.append_event(key, 'lifecycle', f'Base setup failed: {setup}\n{prepared.output}')
                return None
        return await run_check(command, path, timeout)

    @staticmethod
    async def _git_output(cwd: Path, *args: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec('git', '-C', str(cwd), *args, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return None
        stdout, _ = await process.communicate()
        return stdout.decode(errors='replace') if process.returncode == 0 else None

    def _baseline_path(self) -> Path | None:
        data_dir = self._setting('data_dir', None)
        return Path(data_dir) / 'baseline-checks.json' if data_dir else None

    def _load_baseline(self) -> dict:
        path = self._baseline_path()
        try:
            return json.loads(path.read_text()) if path and path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _remember_baseline(self, sha: str, command: str, entry: dict) -> None:
        self._baseline.setdefault(sha, {})[command] = entry
        if path := self._baseline_path():
            saved = self._load_baseline()
            saved.setdefault(sha, {})[command] = entry
            saved = dict(list(saved.items())[-5:])  # base commits move on; keep the latest few
            try:
                path.write_text(json.dumps(saved))
            except OSError:
                log.warning("could not save baseline check results")

    @staticmethod
    def _json(task: dict | None, column: str) -> list:
        return json.loads((task or {}).get(column) or '[]')

    def request_recheck(self, key: str) -> None:
        """Rerun the local checks and UI capture on a PARTIAL task's worktree without an agent turn.

        For work the agent finished but could not prove, e.g. checks that timed out or were blocked: when they pass,
        the task goes on to review as COMPLETED; only a real failure wakes the agent, in its own session."""
        task = self.database.get_task(key)
        if not task or task['state'] not in ('needs_human', 'failed', 'interrupted'):
            raise ValueError('Task is not waiting for you')
        report = parse_report(task.get('report') or '') if task.get('report') else None
        if not report or report['status'] not in ('PARTIAL', 'COMPLETED'):
            raise ValueError('Only a PARTIAL or COMPLETED report can be rechecked')
        if report.get('questions'):
            raise ValueError('Answer the open questions instead; a recheck cannot decide them')
        if not task.get('worktree') or not Path(task['worktree']).is_dir():
            raise ValueError('The worktree is gone; resume or retry the task instead')
        self.database.update_task(key, TaskState(task['state']), recheck=1)
        self.database.append_event(key, 'lifecycle', 'Recheck requested: rerunning checks and UI capture without the agent')
        self.database.request_resume(key)
        self.wake_event.set()

    def _recheck_result(self, key: str, task: dict, session_id: str | None) -> CodexRunResult:
        """The agent's last report, offered to verification as COMPLETED; failing checks still downgrade it."""
        report = parse_report(task.get('report') or '') or {}
        previous = '; '.join(report.get('blockers') or []) or report.get('status', '')
        note = ('## Kiểm tra lại bởi automation\n'
                'Chạy lại check và chụp UI trên worktree hiện có, không gọi agent. Trạng thái trước đó: '
                f"{report.get('status')}" + (f' — {previous}' if previous else ''))
        report.update(status='COMPLETED', blockers=[], sandbox_blocked_only=False,
                      report=(report.get('report') or '').rstrip() + '\n\n' + note)
        self.database.append_event(key, 'lifecycle', 'Rechecking the existing changes without an agent turn')
        return CodexRunResult(outcome=RunOutcome.SUCCEEDED, session_id=session_id,
                              final_message=json.dumps(report, ensure_ascii=False))

    async def _resume_unblocked(self) -> None:
        """Resume BLOCKED_DEPENDENCY tasks once every issue they named is Done in Jira.

        The worktree is first moved onto the latest base (keeping its uncommitted work) so the merged dependency is
        there. Each set of dependencies triggers one automatic resume; a task that blocks again waits for a human."""
        interval = self._setting('jira_poll_seconds', 600)
        if self._last_unblock_check is not None and time.monotonic() - self._last_unblock_check < interval:
            return
        self._last_unblock_check = time.monotonic()
        for task in self.database.list_tasks(TaskState.NEEDS_HUMAN):
            key = task['issue_key']
            report = json.loads(task['report']) if task.get('report') else {}
            if report.get('status') != 'BLOCKED_DEPENDENCY' or report.get('questions'):
                continue
            text = ' '.join(report.get('blockers') or []) + ' ' + (report.get('summary') or '')
            needed = sorted(set(ISSUE_KEY.findall(text)) - {key})
            if not needed or self._json(task, 'auto_resumed') == needed:
                continue
            try:
                issue = await self.jira.get_issue(key)
                flag_field = await self.jira.flag_field() if hasattr(self.jira, 'flag_field') else None
                if issue.blockers(flag_field):
                    continue  # Jira links still name an unfinished blocker
                waiting = [other for other in needed if not getattr((await self.jira.get_issue(other)).fields.status, 'done', False)]
            except Exception as exc:
                log.warning('could not check dependencies of %s: %s', key, exc)
                continue
            if waiting:
                continue
            self.database.update_task(key, TaskState.NEEDS_HUMAN, auto_resumed=needed)
            if task.get('worktree') and Path(task['worktree']).is_dir():
                problem = await self.worktrees.update_base(Path(task['worktree']))
                if problem:
                    self.database.append_event(key, 'lifecycle', f"{', '.join(needed)} done, but the worktree was not "
                                               f'updated onto the latest base: {problem}. Update it and resume.')
                    self.notify(f'{key}: dependency done', 'Worktree needs a manual update before resuming')
                    continue
            self.database.append_event(key, 'lifecycle', f"{', '.join(needed)} done in Jira; worktree on the latest "
                                       'base, resuming automatically')
            self.database.request_resume(key)
            self.wake_event.set()

    def _resume_preamble(self, task: dict, approvals: bool = True) -> str:
        """Tell the resumed Codex session what changed since it stopped."""
        parts = ['Continue the interrupted task in this existing worktree. Inspect and preserve existing changes, '
                 'finish remaining work and run only narrow checks; the automation reruns the full checks and UI capture.']
        report = json.loads(task['report']) if task.get('report') else None
        if report and report.get('status') != 'COMPLETED':
            blockers = '; '.join(report.get('blockers') or [])
            parts.append(f"Your previous run ended {report['status']}" + (f': {blockers}' if blockers else '.'))
        if self._json(task, 'decisions'):
            parts.append('Your questions have now been answered; see HUMAN DECISIONS below and continue accordingly.')
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
                {'answered_at': datetime.now(timezone.utc).isoformat(), 'by': 'human', 'answers': answered, 'note': note}]
        if allowed:
            fields['allowed_approvals'] = self._json(task, 'allowed_approvals') + allowed
        self.database.update_task(key, TaskState(task['state']), **fields)
        self.database.append_event(key, 'lifecycle', f'Human answered {len(answered)} question(s)'
                                   + (f', allowed {len(allowed)} request(s)' if allowed else '') + '; resuming')
        self.database.request_resume(key)
        self.wake_event.set()

    def _deciding_enabled(self) -> bool:
        return bool(self._setting('claude_decide', False) and self._available('decide'))

    def _pass_name(self, role: str) -> str:
        return AGENT_NAMES[self._role(role)['agent']]

    async def _decide(self, key: str, task_text: str, plan: str | None, questions: list[dict],
                      worktree: Path) -> tuple[list[dict], str | None]:
        """The decision agent answers open questions as the product owner; returns the answers (possibly fewer)
        and a summary."""
        name = self._pass_name('decide')
        self.database.append_event(key, 'lifecycle', f'{name} deciding {len(questions)} open question(s) as product owner')
        instructions = self._setting('decide_instructions', None) or DEFAULT_DECIDE_INSTRUCTIONS
        outcome = await self._ask('decide', key, decide_prompt(task_text, plan, questions, instructions), worktree,
                                  DECIDE_SCHEMA)
        if not outcome.ok:
            self.database.append_event(key, 'lifecycle', f'{name} could not decide: {outcome.error}')
            return [], None
        asked = {str(item.get('id')): item for item in questions}
        answers = []
        for item in outcome.data.get('decisions') or []:
            answer = str(item.get('answer') or '').strip()
            if not answer:
                continue
            question = asked.get(str(item.get('id')), {}).get('question') or str(item.get('question') or '')
            answers.append({'question': question, 'answer': answer, 'rationale': str(item.get('rationale') or ''),
                            'assumptions': [str(text) for text in item.get('assumptions') or [] if str(text).strip()],
                            'acceptance': [str(text) for text in item.get('acceptance') or [] if str(text).strip()],
                            'confidence': item.get('confidence') or 'low',
                            'risk': item.get('risk') if item.get('risk') in RISKS else 'MEDIUM',
                            'sources': [str(source) for source in item.get('sources') or []]})
        summary = str(outcome.data.get('summary') or '').strip() or None
        self.database.append_event(key, 'lifecycle', f'{name} decided {len(answers)}/{len(questions)} question(s)'
                                   + (f': {summary}' if summary else ''))
        return answers, summary

    def _record_decisions(self, key: str, answers: list[dict], summary: str | None, stage: str) -> None:
        task = self.database.get_task(key) or {}
        decision = {'answered_at': datetime.now(timezone.utc).isoformat(), 'by': 'agent', 'stage': stage,
                    'answers': answers, 'note': summary}
        self.database.update_task(key, TaskState(task.get('state') or TaskState.CODING.value),
                                  decisions=self._json(task, 'decisions') + [decision])

    async def _decide_plan(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                           questions: list[str]) -> None:
        """Settle the plan's open questions before anyone codes, so the coder starts with clear requirements."""
        self.database.update_task(issue.key, TaskState.CODING, phase='deciding')
        asked = [{'id': f'p{index}', 'question': question, 'context': '', 'options': []}
                 for index, question in enumerate(questions, 1)]
        answers, summary = await self._decide(issue.key, self._task_text(issue, task), plan, asked, worktree)
        if answers:
            self._record_decisions(issue.key, answers, summary, 'plan')

    async def _settle(self, issue: JiraIssue, worktree: Path, plan: str | None, result: CodexRunResult, fix,
                      agent: str, scope: str = '') -> CodexRunResult:
        """When the coder stops on questions, the decision agent answers them and the coder continues, for a bounded number of rounds."""
        if not self._deciding_enabled():
            return result
        rounds = self._setting('claude_decide_rounds', 2)
        for attempt in range(rounds):
            report = parse_report(result.final_message) if result.outcome == RunOutcome.SUCCEEDED else None
            questions = (report or {}).get('questions') or []
            if not report or report['status'] == 'COMPLETED' or not questions:
                break
            self.database.update_task(issue.key, TaskState.CODING, phase='deciding')
            task = self.database.get_task(issue.key) or {}
            answers, summary = await self._decide(issue.key, self._task_text(issue, task, scope), plan, questions, worktree)
            if not answers:
                break
            self._record_decisions(issue.key, answers, summary, 'report')
            self.database.update_task(issue.key, TaskState.CODING, phase='coding')
            self.database.append_event(issue.key, 'lifecycle',
                                       f'{agent} continuing with the decisions (round {attempt + 1}/{rounds})')
            result = await fix(decisions_prompt(answers, output_contract(self._setting('approval_timeout_seconds', 0))),
                               result)
            result = await self._verify(issue.key, worktree, result, fix, agent)
        return result

    def request_decide(self, key: str) -> None:
        """Let the decision agent answer a waiting task's questions in the background, then resume it with them."""
        if not self._available('decide'):
            raise ValueError(f"{CODER_NAMES[self._role('decide')['agent']]} is not enabled")
        task = self.database.get_task(key)
        if not task or task['state'] not in ('needs_human', 'failed', 'interrupted'):
            raise ValueError('Task is not waiting for you')
        if not (json.loads(task['report']) if task.get('report') else {}).get('questions'):
            raise ValueError('The task has no open questions')
        if key in self._deciding:
            raise ValueError('The agent is already deciding this task')
        self._deciding.add(key)
        job = asyncio.get_running_loop().create_task(self._decide_waiting(key))
        self._background.add(job)
        job.add_done_callback(self._background.discard)

    def _decide_backlog(self) -> None:
        """Hand tasks waiting on questions to the decision agent once, e.g. those stuck from before deciding was enabled.

        A task the agent already decided for stays with the human, so a coder that keeps asking cannot loop forever."""
        if not self._deciding_enabled():
            return
        for task in self.database.list_tasks(TaskState.NEEDS_HUMAN):
            key = task['issue_key']
            if key in self._deciding or any(item.get('by') == 'agent' for item in self._json(task, 'decisions')):
                continue
            if not (json.loads(task['report']) if task.get('report') else {}).get('questions'):
                continue
            self.database.append_event(key, 'lifecycle', 'Handing the open questions to the agent automatically')
            self.request_decide(key)

    async def _decide_waiting(self, key: str) -> None:
        try:
            task = self.database.get_task(key) or {}
            questions = json.loads(task['report']).get('questions') or []
            issue = await self.jira.get_issue(key)
            worktree = Path(task['worktree']) if task.get('worktree') and Path(task['worktree']).is_dir() \
                else Path(self.settings.repository)
            answers, summary = await self._decide(key, self._task_text(issue, task), task.get('plan'), questions, worktree)
            task = self.database.get_task(key) or {}
            if task.get('state') not in ('needs_human', 'failed', 'interrupted'):
                return  # the human answered or discarded it meanwhile
            if not answers:
                self.notify(f'{key}: agent could not decide', 'Answer the questions yourself')
                return
            self._record_decisions(key, answers, summary, 'manual')
            self.database.append_event(key, 'lifecycle', f'Agent answered {len(answers)} question(s); resuming')
            self.database.request_resume(key)
            self.wake_event.set()
        except Exception as exc:
            log.exception("deciding %s failed", key)
            self.database.append_event(key, 'error', f'Agent decision failed: {exc}')
        finally:
            self._deciding.discard(key)

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

    async def _ask(self, role: str, key: str, prompt: str, worktree: Path, schema: dict, *, check_quota: bool = True,
                   agent: str | None = None, credit: tuple[str, ...] | None = None, **options) -> ClaudeResult:
        """Run one headless pass with the role's agent and record the tokens it used and who did the job.

        Claude passes wait out the subscription reserve; agent overrides the configured one for resumed work."""
        spec = self._role(role)
        if agent and agent != spec['agent']:
            spec = {'agent': agent, 'model': None, 'effort': None}
        runner = self._runner_for(spec['agent'])
        if runner is None:
            return ClaudeResult(ok=False, error=f"{CODER_NAMES.get(spec['agent'], spec['agent'])} is not enabled")
        if spec['agent'] == 'claude' and check_quota and (blocked := await self._claude_quota_blocked()):
            return ClaudeResult(ok=False, error=blocked)
        for name in ('model', 'effort'):
            if spec[name]:
                options.setdefault(name, spec[name])
        outcome = await runner.run(prompt, worktree, schema=schema,
                                   on_event=lambda event: self.database.append_event(key, event['type'], event['message']),
                                   **options)
        if spec['agent'] == 'claude':
            current = self.database.get_task(key) or {}
            usage = json.loads(current.get('claude_usage') or '{}')
            usage = {'input_tokens': usage.get('input_tokens', 0) + outcome.input_tokens,
                     'output_tokens': usage.get('output_tokens', 0) + outcome.output_tokens,
                     'passes': usage.get('passes', 0) + 1}
            # A decision pass may run for a task waiting in the human queue; keep whatever state it is in.
            self.database.update_task(key, TaskState(current.get('state') or TaskState.CODING.value), claude_usage=usage)
        self.database.append_event(key, f"{spec['agent']}.usage",
                                   f'{outcome.input_tokens:,} input / {outcome.output_tokens:,} output tokens')
        if outcome.ok:
            self._credit(key, credit or (PASS_ROLES.get(role, role),), spec['agent'], options.get('model'),
                         options.get('effort'), outcome)
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

    async def _plan(self, issue: JiraIssue, task: dict, worktree: Path) -> tuple[str | None, str | None, list[str]]:
        """Ask the planning agent for an implementation plan and the issue's area; the coder plans alone when it is
        unavailable."""
        name = self._pass_name('plan')
        self.database.append_event(issue.key, 'lifecycle', f'{name} planning')
        outcome = await self._ask('plan', issue.key, plan_prompt(self._task_text(issue, task),
                                                                 self.settings.task_instructions), worktree, PLAN_SCHEMA)
        if not outcome.ok:
            self.database.append_event(issue.key, 'lifecycle', f'{name} plan unavailable; '
                                       f"{CODER_NAMES[self._role('backend')['agent']]} plans alone: {outcome.error}")
            return None, None, []
        plan, area = plan_markdown(outcome.data), outcome.data.get('area')
        self.database.update_task(issue.key, TaskState.CODING, plan=plan, area=area)
        self.database.append_event(issue.key, 'lifecycle', f"{name} plan ready ({area or 'area unknown'}): "
                                   + outcome.data.get('summary', ''))
        return plan, area, [str(item) for item in outcome.data.get('questions') or [] if str(item).strip()]

    async def _classify(self, issue: JiraIssue, task: dict, worktree: Path) -> str | None:
        """Ask whether the issue is frontend, backend or full-stack work; None leaves it all to the backend agent."""
        name = self._pass_name('classify')
        self.database.append_event(issue.key, 'lifecycle', f'{name} classifying the issue')
        outcome = await self._ask('classify', issue.key, classify_prompt(self._task_text(issue, task)), worktree,
                                  CLASSIFY_SCHEMA)
        if not outcome.ok:
            self.database.append_event(issue.key, 'lifecycle', 'Could not classify the issue; '
                                       f"{CODER_NAMES[self._role('backend')['agent']]} codes it: {outcome.error}")
            return None
        area = outcome.data.get('area')
        self.database.update_task(issue.key, TaskState.CODING, area=area)
        self.database.append_event(issue.key, 'lifecycle', f"Issue classified as {area}: {outcome.data.get('reason', '')}")
        return area

    async def _code_frontend(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                             backend: dict | None, *, resume: bool, codex_session: str | None,
                             backend_agent: str = 'codex', recheck: bool = False) -> CodexRunResult | None:
        """The frontend agent implements the UI in the worktree; None when it cannot run now."""
        key = issue.key
        coder = await self._coder('frontend', key, task, worktree, resume=resume, codex_session=codex_session,
                                  roles=('frontend',))
        if coder is None:
            return None
        self.database.update_task(key, TaskState.CODING, phase='frontend')
        self.database.append_event(key, 'lifecycle', f'{coder.name} resuming the frontend work' if resume
                                   else f'{coder.name} implementing the frontend')
        prompt = self._prompt(issue, task, plan, frontend_scope(backend, CODER_NAMES[backend_agent]))
        if resume:
            prompt = self._resume_preamble(task, approvals=coder.agent == 'codex') + prompt
        result = self._recheck_result(key, task, codex_session) if recheck else await coder.start(prompt)
        if result is None:
            return None
        result = await self._verify(key, worktree, result, coder.fix, coder.name)
        result = await self._settle(issue, worktree, plan, result, coder.fix, coder.name)
        report = parse_report(result.final_message) if backend and result.outcome == RunOutcome.SUCCEEDED else None
        if report:
            merged = merge_reports(backend, report, CODER_NAMES[backend_agent], coder.name)
            result = result.model_copy(update={'final_message': json.dumps(merged, ensure_ascii=False)})
        return result

    async def _frontend_after_backend(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                                      backend: dict, result: CodexRunResult, coder: Coder) -> CodexRunResult:
        """Second half of a full-stack issue: the frontend agent codes the UI, or the backend agent continues
        when it cannot run."""
        self.database.update_task(issue.key, TaskState.CODING, backend_report=backend, session_id=result.session_id)
        frontend = await self._code_frontend(issue, task, worktree, plan, backend, resume=False,
                                             codex_session=result.session_id, backend_agent=coder.agent)
        if frontend is not None:
            return frontend
        self.database.update_task(issue.key, TaskState.CODING, phase='coding')
        self.database.append_event(issue.key, 'lifecycle', f'{coder.name} implementing the frontend instead')
        coder.roles = ('frontend',)
        frontend = await coder.fix(
            'The backend part is done. Now implement the frontend part of the issue in this worktree on top of it, '
            'following the same rules and plan, declare the changed screens in "ui_captures" and report on the whole issue.\n\n'
            + output_contract(self._setting('approval_timeout_seconds', 0)), result)
        frontend = await self._verify(issue.key, worktree, frontend, coder.fix, coder.name)
        return await self._settle(issue, worktree, plan, frontend, coder.fix, coder.name)

    async def _review_loop(self, issue: JiraIssue, task: dict, worktree: Path, plan: str | None,
                           result: CodexRunResult, coder: Coder,
                           scope: str = '') -> tuple[CodexRunResult, dict | None, str | None]:
        """The review agent reviews COMPLETED work; the coder fixes requested changes for a bounded number of rounds.

        Returns the final result, the last review and, when the latest change went unreviewed, why."""
        rounds, review, skipped = self._setting('claude_review_rounds', 1), None, None
        name = self._pass_name('review')
        for attempt in range(rounds + 1):
            report = parse_report(result.final_message) if result.outcome == RunOutcome.SUCCEEDED else None
            if not report or report['status'] != 'COMPLETED':
                break
            self.database.update_task(issue.key, TaskState.CODING, phase='reviewing')
            self.database.append_event(issue.key, 'lifecycle', f'{name} reviewing changes')
            outcome = await self._ask('review', issue.key,
                                      review_prompt(self._task_text(issue, task, scope), plan, report, coder.name),
                                      worktree, REVIEW_SCHEMA)
            if not outcome.ok:
                # Unreviewed work goes to the human queue rather than looking as vetted as reviewed work.
                self.database.append_event(issue.key, 'lifecycle', f'{name} review unavailable: {outcome.error}')
                skipped = outcome.error or f'{name} review unavailable'
                if review is not None:
                    # An earlier review describes code the coder has since changed; keep it only in the log.
                    self.database.update_task(issue.key, TaskState.CODING, review=None)
                review = None
                break
            review = outcome.data
            self.database.update_task(issue.key, TaskState.CODING, review=review)
            self.database.append_event(issue.key, 'lifecycle', f"{name} review: {review['verdict']} — {review.get('summary', '')}")
            if review['verdict'] != 'REQUEST_CHANGES' or attempt == rounds:
                break
            self.database.update_task(issue.key, TaskState.CODING, phase='fixing')
            self.database.append_event(issue.key, 'lifecycle', f'{coder.name} fixing review findings (round {attempt + 1}/{rounds})')
            result = await coder.fix(fix_prompt(review, output_contract(self._setting('approval_timeout_seconds', 0))), result)
            result = await self._verify(issue.key, worktree, result, coder.fix, coder.name)
        return result, review, skipped

    def _finish(self, key: str, result: CodexRunResult, review: dict | None = None,
                review_skipped: str | None = None) -> TaskState:
        if result.outcome != RunOutcome.SUCCEEDED:
            self.database.update_task(key, TaskState.CODING, session_id=result.session_id,
                                      result=result.model_dump(mode="json"))
            self._attempt_failed(key, result.error or result.outcome.value)
            return TaskState.INTERRUPTED
        report = parse_report(result.final_message)
        if report:
            report = self._check_images(key, report)
        status = report['status'] if report else None
        if status == 'COMPLETED' and review and review.get('verdict') == 'REQUEST_CHANGES':
            # The coder says done but the reviewer still sees blocking issues after the allowed fix rounds.
            state = TaskState.NEEDS_HUMAN
            error = f"{self._pass_name('review')} review requested changes: " + (review.get('summary') or 'see review findings')
        elif status == 'COMPLETED' and review_skipped:
            # Review is enabled but did not run; check the change (and its screenshots) by hand.
            state = TaskState.NEEDS_HUMAN
            error = f"{self._pass_name('review')} review skipped ({review_skipped}); review the change and screenshots manually"
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
        decisions = self._json(self.database.get_task(key), 'decisions')
        agent_decided = sum(len(item.get('answers') or []) for item in decisions if item.get('by') == 'agent')
        if report and agent_decided:
            report = {**report, 'report': (report.get('report') or '').rstrip() + '\n\n' + decisions_report(decisions)}
            self.database.append_event(key, 'lifecycle', f'{agent_decided} decision(s) made by the agent; listed in the report for review')
        self.database.append_event(key, 'lifecycle', f'Task {state.value}: {error or summary}')
        self.database.update_task(key, state, session_id=result.session_id, result=result.model_dump(mode="json"),
                                  report=report, error=error, attempts=0, next_attempt_at=None,
                                  **({'review': review} if review is not None else {}))
        self.notify(f'{key}: ' + ('ready for review' if state == TaskState.REVIEW else 'needs you')
                    + (f' · {agent_decided} agent decision(s) to check' if agent_decided else ''),
                    error or summary or 'Codex finished')
        return state

    def _check_images(self, key: str, report: dict) -> dict:
        """Mark each reported image slot whose file is not in the worktree, whatever source the agent claimed,
        and add a slot for every image the change uses that the report does not list.

        resolved_path is absolute so the UI can re-check it live once the human saves a generated image there."""
        worktree = (self.database.get_task(key) or {}).get('worktree')
        if not worktree:
            return report
        root = Path(worktree).resolve()
        files: list[str] | None = None

        def resolve(relative: str, near: str = '') -> Path | None:
            nonlocal files
            relative = relative.strip().split('?', 1)[0].split('#', 1)[0]
            candidates = [root / near / relative] if near and relative.startswith('.') else []
            candidates.append(root / relative.lstrip('/'))
            for candidate in candidates:
                target = candidate.resolve()
                if target.is_relative_to(root) and target.is_file():
                    return target
            first = candidates[0].resolve()
            if not first.is_relative_to(root):
                return None  # never look outside the worktree
            # Agents sometimes give the path relative to a portal (public/images/...); match it as a suffix.
            if files is None:
                files = self._worktree_files(root)
            bare = relative.lstrip('/')
            while bare.startswith(('./', '../')):
                bare = bare.split('/', 1)[1]
            matches = [name for name in files if name == bare or name.endswith('/' + bare)]
            return root / matches[0] if len(matches) == 1 else first

        images, missing = [], []
        for image in report.get('images') or []:
            relative = str(image.get('path') or '').strip()
            target = resolve(relative) if relative else None
            present = bool(target and target.is_file())
            images.append({**image, 'resolved_path': str(target) if target else '', 'missing': not present})
            if not present:
                missing.append(image.get('placement') or relative or '(no path)')

        undeclared = []
        for found in changed_image_references(root):
            if is_declared(found, images):
                continue
            web = found.reference.startswith(('http://', 'https://'))
            target = None if web else resolve(found.reference, str(Path(found.file).parent))
            placement = found.file + (f':{found.line}' if found.line else '')
            images.append({'placement': placement, 'source': 'undeclared', 'path': found.reference,
                           'purpose': 'Used by the change but not listed in the report; confirm it belongs and supply the file',
                           'aspect_ratio': '', 'url': found.reference if web else '', 'license': '', 'prompt': '',
                           'resolved_path': str(target) if target else '', 'missing': not web and not (target and target.is_file()),
                           'undeclared': True})
            undeclared.append(f'{found.reference} ({placement})')
        if missing:
            self.database.append_event(key, 'lifecycle', f'{len(missing)} image file(s) missing: ' + '; '.join(missing))
        if undeclared:
            self.database.append_event(key, 'lifecycle', f'{len(undeclared)} image(s) used but not listed in the report: '
                                       + '; '.join(undeclared))
        return {**report, 'images': images} if images else report

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
                lines += [decision_lines(item) for item in decision.get('answers') or []]
                if decision.get('note'):
                    lines.append(f"- Note: {decision['note']}")
            context += '\nHUMAN DECISIONS (authoritative answers to earlier questions; follow them):\n' + '\n'.join(lines) + '\n'
        return f"""Implement Jira issue {issue.key}: {issue.summary}

Description:
{description}
{context}{scope}"""

    def _prompt(self, issue: JiraIssue, task: dict | None = None, plan: str | None = None, scope: str = '') -> str:
        checks = checks_section(commands) if (commands := self._verify_commands()) else ''
        shots = capture_section() if self._setting('ui_capture', True) else ''
        return f"""{self._task_text(issue, task, scope)}{plan_section(plan) if plan else ''}
{self.settings.task_instructions}
{checks}{shots}
{output_contract(self._setting('approval_timeout_seconds', 0))}
"""
