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
from jira_codex_agent.storage.database import Database, TaskState

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
    # Codex still finishes, but unreviewed work waits for a human instead of looking vetted.
    assert len(prompts) == 1 and task['plan'] is None and task['review'] is None
    assert task['state'] == 'needs_human' and 'Claude review skipped (offline)' in task['error']
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
    assert task['state'] == 'needs_human' and 'IMPLEMENTATION PLAN' not in prompts[0]
    assert 'review the change and screenshots manually' in task['error']
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
    assert len(prompts) == 2 and task['state'] == 'needs_human' and task['review'] is None


@pytest.mark.asyncio
async def test_review_disabled_keeps_completed_work_in_review(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [], [COMPLETED], claude_review=False)
    await orchestrator.process(issue())
    assert db.get_task('TM-1')['state'] == 'review'


def test_review_prompt_asks_claude_to_inspect_ui_screenshots():
    from jira_codex_agent.claude.prompts import review_prompt
    prompt = review_prompt('Implement TM-1', None, {'summary': 'Done', 'screenshots': ['.ui-evidence/curriculum-360.png']})
    assert '.ui-evidence/curriculum-360.png' in prompt and 'Read tool' in prompt and 'hover' in prompt
    assert '- (none)' in review_prompt('Implement TM-1', None, {'summary': 'Done'})


FRONTEND_REPORT = {'status': 'COMPLETED', 'summary': 'Screen built', 'blockers': [], 'questions': [],
                   'tests': [{'command': 'npm test', 'result': 'PASS'}], 'screenshots': ['.ui-evidence/a.png'],
                   'branch': 'b', 'report': 'fe'}
BACKEND_DONE = json.dumps({'status': 'COMPLETED', 'summary': 'API added', 'blockers': [], 'questions': [],
                           'tests': [{'command': 'pytest', 'result': 'PASS'}], 'screenshots': [], 'branch': 'b', 'report': 'be'})


def classified(area):
    return {'area': area, 'reason': 'because'}


@pytest.mark.asyncio
async def test_frontend_issue_is_coded_by_claude_and_skips_review(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [classified('frontend'), FRONTEND_REPORT], [],
                                      claude_frontend=True, claude_review=True, max_task_seconds=3600)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert prompts == [] and task['state'] == 'review' and task['area'] == 'frontend' and task['phase'] == 'frontend'
    coding = orchestrator.claude.run.await_args_list[1]
    assert coding.kwargs['write'] is True and coding.kwargs['timeout'] == 3600
    assert coding.kwargs['session_id'] == task['claude_session_id']
    assert 'YOU ARE THE IMPLEMENTER' in coding.args[0] and 'Do it well.' in coding.args[0]
    assert json.loads(task['report'])['screenshots'] == ['.ui-evidence/a.png']


@pytest.mark.asyncio
async def test_backend_issue_stays_with_codex(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [classified('backend'), APPROVE], [COMPLETED],
                                      claude_frontend=True, claude_review=True)
    await orchestrator.process(issue())
    assert len(prompts) == 1 and 'BACKEND ONLY' not in prompts[0]
    assert db.get_task('TM-1')['state'] == 'review' and orchestrator.claude.run.await_count == 2


@pytest.mark.asyncio
async def test_fullstack_issue_splits_backend_codex_review_then_claude_frontend(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [{**PLAN, 'area': 'fullstack'}, APPROVE, FRONTEND_REPORT], [BACKEND_DONE],
                                      claude_plan=True, claude_frontend=True, claude_review=True)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    calls = orchestrator.claude.run.await_args_list
    assert len(prompts) == 1 and 'BACKEND ONLY' in prompts[0] and 'BACKEND ONLY' in calls[1].args[0]
    assert 'FRONTEND PART' in calls[2].args[0] and 'API added' in calls[2].args[0] and calls[2].kwargs['write']
    report = json.loads(task['report'])
    assert task['state'] == 'review' and task['session_id'] == 'thread-1'
    assert '## Backend (Codex)\nbe' in report['report'] and '## Frontend (Claude Code)\nfe' in report['report']
    assert [t['command'] for t in report['tests']] == ['pytest', 'npm test']


@pytest.mark.asyncio
async def test_fullstack_backend_with_unresolved_review_never_reaches_frontend(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [classified('fullstack'), CHANGES], [BACKEND_DONE],
                                claude_frontend=True, claude_review=True, claude_review_rounds=0)
    await orchestrator.process(issue())
    assert db.get_task('TM-1')['state'] == 'needs_human' and orchestrator.claude.run.await_count == 2


@pytest.mark.asyncio
async def test_frontend_falls_back_to_codex_when_claude_quota_is_reserved(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [classified('frontend')], [COMPLETED], claude_frontend=True)
    orchestrator._claude_quota_blocked = AsyncMock(side_effect=[None, 'Claude 5-hour quota at 10% remaining'])
    await orchestrator.process(issue())
    assert len(prompts) == 1 and 'IMPLEMENTER' not in prompts[0] and db.get_task('TM-1')['state'] == 'review'


@pytest.mark.asyncio
async def test_interrupted_frontend_resumes_the_claude_session(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [FRONTEND_REPORT], [], claude_frontend=True)
    db.update_task('TM-1', TaskState.INTERRUPTED, worktree=str(tmp_path), phase='frontend', area='frontend',
                   claude_session_id='s-1')
    await orchestrator.process(issue(), resume=db.get_task('TM-1'))
    call = orchestrator.claude.run.await_args
    assert prompts == [] and call.kwargs['resume'] == 's-1' and 'Continue the interrupted task' in call.args[0]
    assert db.get_task('TM-1')['state'] == 'review'


def test_coding_pass_gets_write_tools_but_not_git_history():
    args = ClaudeRunner('claude', max_turns=30).arguments({}, write=True, session_id='abc')
    assert 'Edit' in args and 'Write' in args and '--session-id' in args and '--max-turns' not in args
    assert 'Bash(git commit:*)' in args[args.index('--disallowedTools'):]
    assert '--resume' in ClaudeRunner('claude').arguments({}, write=True, resume='abc')


def test_review_prompt_lists_image_slots_and_report_schema_requires_them():
    from jira_codex_agent.claude.prompts import review_prompt
    from jira_codex_agent.codex.report import REPORT_SCHEMA, parse_report
    image = {'placement': 'Home hero', 'purpose': 'welcome', 'source': 'generate', 'path': 'public/images/home/hero.webp',
             'aspect_ratio': '16:9', 'url': '', 'license': '', 'prompt': 'Friendly flat illustration of a classroom'}
    prompt = review_prompt('Task', None, {'summary': 's', 'images': [image]})
    assert 'Home hero [generate] public/images/home/hero.webp 16:9: Friendly flat illustration' in prompt
    assert 'guessed or unverified' in prompt
    assert 'images' in REPORT_SCHEMA['required'] and parse_report(COMPLETED)['images'] == []


@pytest.mark.asyncio
async def test_finished_report_marks_image_files_missing_or_present(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'apps/web/public/images').mkdir(parents=True)
    (tmp_path / 'apps/web/public/images/logo.webp').write_bytes(b'x')
    (tmp_path / 'hero.webp').write_bytes(b'x')
    slot = {'placement': '', 'purpose': '', 'aspect_ratio': '1:1', 'url': '', 'license': '', 'prompt': ''}
    report = json.loads(FRONTEND_REPORT_JSON := json.dumps({**FRONTEND_REPORT, 'images': [
        {**slot, 'placement': 'Root', 'source': 'repository', 'path': 'hero.webp'},
        {**slot, 'placement': 'Portal', 'source': 'web', 'path': 'public/images/logo.webp'},
        {**slot, 'placement': 'Hero', 'source': 'generate', 'path': 'apps/web/public/images/home.webp', 'prompt': 'p'},
        {**slot, 'placement': 'Escape', 'source': 'repository', 'path': '../../etc/hosts'},
    ]}))
    db, orchestrator, _ = setup(tmp_path, [], [FRONTEND_REPORT_JSON])
    await orchestrator.process(issue())
    images = json.loads(db.get_task('TM-1')['report'])['images']
    assert [image['missing'] for image in images] == [False, False, True, True]
    assert images[1]['resolved_path'] == str((tmp_path / 'apps/web/public/images/logo.webp').resolve())
    assert images[2]['resolved_path'].endswith('apps/web/public/images/home.webp') and images[3]['resolved_path'] == ''
    assert any('2 image file(s) missing: Hero; Escape' in e['message'] for e in db.events('TM-1'))
    assert len(images) == len(report['images'])
