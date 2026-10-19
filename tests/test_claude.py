import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from datetime import datetime, timezone

from jira_codex_agent.claude.prompts import PLAN_SCHEMA
from jira_codex_agent.claude.quota import parse_usage
from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow
from jira_codex_agent.claude.runner import ClaudeResult, ClaudeRunner
from jira_codex_agent.codex.models import CodexRunResult, RunOutcome
from jira_codex_agent.storage.database import Database

from test_unattended import agent, issue

COMPLETED = json.dumps({'status': 'COMPLETED', 'summary': 'Done', 'blockers': [], 'tests': [], 'branch': 'b', 'report': 'r'})
PLAN = {'summary': 'Add the endpoint', 'steps': ['Edit api.py'], 'files': ['api.py'], 'tests': ['pytest'],
        'risks': [], 'questions': []}
CHANGES = {'verdict': 'REQUEST_CHANGES', 'summary': 'Missing tenant check',
           'findings': [{'severity': 'blocker', 'location': 'api.py:10', 'issue': 'No tenant filter', 'fix': 'Filter'}]}
APPROVE = {'verdict': 'APPROVE', 'summary': 'Looks good', 'findings': []}


def fake_cli(tmp_path, lines, exit_code=0):
    script = tmp_path / 'claude'
    body = '\n'.join(f"printf '%s\\n' '{json.dumps(line)}'" for line in lines)
    script.write_text(f'#!/bin/sh\ncat > "{tmp_path}/stdin.txt"\necho "$@" > "{tmp_path}/args.txt"\n{body}\nexit {exit_code}\n')
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_runner_returns_structured_output_and_logs_tools(tmp_path):
    command = fake_cli(tmp_path, [
        {'type': 'system', 'subtype': 'init'},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'a.py'}},
                                                      {'type': 'text', 'text': 'Planning'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's1', 'total_cost_usd': 0.1,
         'usage': {'input_tokens': 10, 'cache_read_input_tokens': 90, 'output_tokens': 5}, 'result': json.dumps(PLAN)},
    ])
    events = []
    result = await ClaudeRunner(command, model='opus', effort='high', max_turns=30).run(
        'Plan TM-1', tmp_path, schema=PLAN_SCHEMA, on_event=events.append)
    assert result.ok and result.data == PLAN and result.session_id == 's1'
    assert (result.input_tokens, result.output_tokens) == (100, 5)
    assert events == [{'type': 'claude.tool', 'message': 'Read: a.py'}, {'type': 'claude.message', 'message': 'Planning'}]
    assert (tmp_path / 'stdin.txt').read_text() == 'Plan TM-1'
    args = (tmp_path / 'args.txt').read_text()
    assert '--permission-mode dontAsk' in args and '--model opus' in args and '--effort high' in args
    assert '--max-turns 30' in args
    assert 'Edit' not in args and 'Write' not in args


@pytest.mark.asyncio
async def test_runner_reports_errors_instead_of_raising(tmp_path):
    command = fake_cli(tmp_path, [{'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
                                   'errors': ['Reached maximum number of turns (30)']}], 1)
    over = await ClaudeRunner(command).run('x', tmp_path, schema=PLAN_SCHEMA)
    assert (over.ok, over.error) == (False, 'Reached maximum number of turns (30)')
    missing = await ClaudeRunner(str(tmp_path / 'nope')).run('x', tmp_path, schema=PLAN_SCHEMA)
    assert not missing.ok and 'Could not start' in missing.error


def setup(tmp_path, claude_results, codex_messages, **settings):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Work')
    orchestrator, notify = agent(db, **settings)
    prompts = []

    async def run(prompt, *_, **__):
        prompts.append(prompt)
        return CodexRunResult(outcome=RunOutcome.SUCCEEDED, final_message=codex_messages.pop(0), session_id='thread-1')

    orchestrator.runner = SimpleNamespace(run=run)
    orchestrator.worktrees = SimpleNamespace(create=AsyncMock(return_value=tmp_path))
    orchestrator.claude = SimpleNamespace(run=AsyncMock(side_effect=[
        ClaudeResult(ok=True, data=item, input_tokens=1000, output_tokens=100) if isinstance(item, dict)
        else ClaudeResult(ok=False, error=item) for item in claude_results]))
    return db, orchestrator, prompts


@pytest.mark.asyncio
async def test_plan_feeds_codex_and_review_findings_are_fixed(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [PLAN, CHANGES, APPROVE], [COMPLETED, COMPLETED],
                                      claude_plan=True, claude_review=True, claude_review_rounds=1)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'review'
    assert 'IMPLEMENTATION PLAN' in prompts[0] and 'Edit api.py' in prompts[0] and 'Edit api.py' in task['plan']
    assert 'No tenant filter' in prompts[1]
    assert json.loads(task['review'])['verdict'] == 'APPROVE'


@pytest.mark.asyncio
async def test_unresolved_review_goes_to_human_queue(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [CHANGES], [COMPLETED], claude_review=True, claude_review_rounds=0)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'needs_human' and 'Missing tenant check' in task['error']
    assert len(prompts) == 1 and 'IMPLEMENTATION PLAN' not in prompts[0]


@pytest.mark.asyncio
async def test_claude_failures_never_block_codex_work(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, ['offline', 'offline'], [COMPLETED], claude_plan=True, claude_review=True)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'review' and task['plan'] is None and task['review'] is None
    assert any('Claude review unavailable' in event['message'] for event in db.events('TM-1'))


USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 11% used · resets Oct 19 at 1:09pm (Asia/Saigon)
Current week (all models): 72% used · resets Oct 25 at 3am (Asia/Saigon)
Current week (Sonnet only): 5% used · resets Oct 25 at 3am (Asia/Saigon)
"""


def test_usage_text_becomes_quota_windows():
    snapshot = parse_usage(USAGE, now=datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc))
    assert (snapshot.primary.used_percent, snapshot.primary.window_minutes) == (11, 300)
    assert snapshot.primary.resets_at == datetime(2026, 10, 19, 6, 9, tzinfo=timezone.utc)
    assert (snapshot.secondary.remaining_percent, snapshot.secondary.window_minutes) == (28, 10080)
    assert snapshot.secondary.resets_at == datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
    # A reset date in January read in late December belongs to next year.
    december = parse_usage('Current session: 1% used · resets Jan 2 at 9am (UTC)', now=datetime(2026, 12, 30, tzinfo=timezone.utc))
    assert december.primary.resets_at == datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)
    assert parse_usage('Unknown command: /usage').error


@pytest.mark.asyncio
async def test_low_claude_quota_skips_passes_and_codex_continues(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [], [COMPLETED], claude_plan=True, claude_review=True,
                                      claude_quota_remaining_threshold=30, claude_quota_weekly_remaining_threshold=30)
    orchestrator.claude_quota = SimpleNamespace(read=AsyncMock(return_value=parse_usage(USAGE)))
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    orchestrator.claude.run.assert_not_awaited()
    assert task['state'] == 'review' and 'IMPLEMENTATION PLAN' not in prompts[0]
    messages = [event['message'] for event in db.events('TM-1')]
    assert any('Claude weekly quota at 28% remaining (stop at 30%)' in message for message in messages)


@pytest.mark.asyncio
async def test_usage_accumulates_when_quota_allows(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [PLAN, APPROVE], [COMPLETED], claude_plan=True, claude_review=True,
                                claude_quota_remaining_threshold=10, claude_quota_weekly_remaining_threshold=10)
    orchestrator.claude_quota = SimpleNamespace(read=AsyncMock(return_value=QuotaSnapshot(
        primary=QuotaWindow(used_percent=50, window_minutes=300), secondary=QuotaWindow(used_percent=50, window_minutes=10080))))
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'review'
    assert json.loads(task['claude_usage']) == {'input_tokens': 2000, 'output_tokens': 200, 'passes': 2}


@pytest.mark.asyncio
async def test_review_that_predates_codex_fix_is_not_kept(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [CHANGES, 'offline'], [COMPLETED, COMPLETED],
                                      claude_review=True, claude_review_rounds=1)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert len(prompts) == 2 and task['state'] == 'review' and task['review'] is None
