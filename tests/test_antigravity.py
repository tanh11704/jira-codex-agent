import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.antigravity.runner import AntigravityRunner, permissions, unavailable
from jira_codex_agent.claude.prompts import PLAN_SCHEMA
from jira_codex_agent.claude.runner import ClaudeResult
from jira_codex_agent.main import agents_in_use

from test_claude import APPROVE, BACKEND_DONE, CHANGES, COMPLETED, FRONTEND_REPORT, PLAN, classified, setup
from test_unattended import issue

PLAN_EVENTS = [
    {'event': 'init', 'conversation_id': 'conv-1', 'init': {'model': 'gemini-4-hydrogen-high'}},
    {'event': 'step_update', 'step_update': {'step_index': 2, 'state': 'ACTIVE', 'step_type': 'tool',
                                             'tool_name': 'view_file', 'tool_info': {'parameters': {'Path': '/w/a.py'}}}},
    {'event': 'step_update', 'step_update': {'step_index': 3, 'state': 'ERROR', 'step_type': 'tool',
                                             'tool_name': 'run_command',
                                             'tool_info': {'parameters': {'CommandLine': 'rm -rf x'},
                                                           'error': {'message': 'permission denied'}}}},
    {'event': 'step_update', 'step_update': {'step_index': 4, 'state': 'ACTIVE', 'step_type': 'agent_response',
                                             'text_delta': 'Reading '}},
    {'event': 'step_update', 'step_update': {'step_index': 4, 'state': 'DONE', 'step_type': 'agent_response',
                                             'text_delta': 'the API'}},
    {'event': 'result', 'result': {'conversation_id': 'conv-1', 'status': 'SUCCESS', 'response': json.dumps(PLAN),
                                   'structured_output': PLAN,
                                   'usage': {'input_tokens': 10, 'cache_read_tokens': 90, 'output_tokens': 5,
                                             'thinking_tokens': 2}}},
]


def fake_agy(tmp_path, lines):
    script = tmp_path / 'agy'
    body = '\n'.join(f"printf '%s\\n' '{json.dumps(line)}'" for line in lines)
    script.write_text(f'#!/bin/sh\nfor arg in "$@"; do printf "%s\\n" "$arg"; done > "{tmp_path}/args.txt"\n{body}\n')
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_runner_returns_structured_output_session_and_logs(tmp_path):
    command = fake_agy(tmp_path, PLAN_EVENTS)
    events, sessions = [], []
    result = await AntigravityRunner(command).run('Plan TM-1', tmp_path, schema=PLAN_SCHEMA, on_event=events.append,
                                                  on_session=sessions.append)
    assert result.ok and result.data == PLAN and result.session_id == 'conv-1' and sessions == ['conv-1']
    assert (result.input_tokens, result.output_tokens, result.model) == (100, 7, 'gemini-4-hydrogen-high')
    assert events == [{'type': 'antigravity.tool', 'message': 'view_file: /w/a.py'},
                      {'type': 'antigravity.tool', 'message': 'run_command failed: rm -rf x\npermission denied'},
                      {'type': 'antigravity.message', 'message': 'Reading the API'}]
    args = (tmp_path / 'args.txt').read_text().splitlines()
    assert args[-1] == '--print=Plan TM-1' and args[args.index('--model') + 1] == 'gemini-4-hydrogen-high'
    rules = json.loads(args[args.index('--settings') + 1])['permissions']
    root = str(tmp_path.resolve())
    assert f'write_file({root})' in rules['deny'] and f'read_file({root})' in rules['allow']
    assert 'command(*)' not in rules['allow'] and '--conversation' not in args


@pytest.mark.asyncio
async def test_runner_resumes_conversation_and_reports_failures(tmp_path):
    command = fake_agy(tmp_path, [{'event': 'result', 'result': {'status': 'ERROR',
                                                                 'error': {'message': 'Quota exhausted for this model'}}}])
    result = await AntigravityRunner(command).run('Fix', tmp_path, schema=PLAN_SCHEMA, write=True, resume='conv-1',
                                                  model='claude-opus-4-6-thinking')
    assert not result.ok and result.error == 'Quota exhausted for this model' and unavailable(result.error)
    args = (tmp_path / 'args.txt').read_text().splitlines()
    assert args[args.index('--conversation') + 1] == 'conv-1'
    assert args[args.index('--model') + 1] == 'claude-opus-4-6-thinking'
    missing = await AntigravityRunner(str(tmp_path / 'nope')).run('x', tmp_path, schema=PLAN_SCHEMA)
    assert not missing.ok and unavailable(missing.error) and not unavailable('TypeError in tests')


def test_coding_permissions_allow_commands_but_not_git_history(tmp_path):
    rules = permissions(tmp_path, write=True)
    assert 'command(*)' in rules['allow'] and f'write_file({tmp_path.resolve()})' in rules['allow']
    assert {'command(git commit)', 'command(git push)', 'command(git reset)'} <= set(rules['deny'])


def antigravity_setup(tmp_path, claude_results, antigravity_results, codex_messages, **settings):
    db, orchestrator, prompts = setup(tmp_path, claude_results, codex_messages, **settings)
    orchestrator.antigravity = SimpleNamespace(run=AsyncMock(side_effect=[
        ClaudeResult(ok=True, data=item, session_id='conv-1', model='gemini-4-hydrogen-high',
                     input_tokens=500, output_tokens=50) if isinstance(item, dict)
        else ClaudeResult(ok=False, error=item) for item in antigravity_results]))
    return db, orchestrator, prompts


@pytest.mark.asyncio
async def test_default_roles_plan_with_claude_backend_codex_frontend_antigravity(tmp_path):
    db, orchestrator, prompts = antigravity_setup(
        tmp_path, [{**PLAN, 'area': 'fullstack'}, APPROVE], [FRONTEND_REPORT], [BACKEND_DONE],
        claude_plan=True, claude_frontend=True, claude_review=True, plan_model='opus',
        codex_model='gpt-6.1-sol', codex_reasoning_effort='high')
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    frontend = orchestrator.antigravity.run.await_args
    assert len(prompts) == 1 and 'BACKEND ONLY' in prompts[0]
    assert 'FRONTEND PART' in frontend.args[0] and 'Another agent (Codex)' in frontend.args[0]
    assert frontend.kwargs['write'] is True
    assert orchestrator.claude.run.await_args_list[0].kwargs['model'] == 'opus'
    agents = json.loads(task['agents'])
    assert {role: entry['agent'] for role, entry in agents.items()} == {
        'plan': 'claude', 'backend': 'codex', 'review': 'claude', 'frontend': 'antigravity'}
    assert agents['backend']['model'] == 'gpt-6.1-sol' and agents['backend']['effort'] == 'high'
    assert agents['frontend']['model'] == 'gemini-4-hydrogen-high' and agents['frontend']['passes'] == 1
    assert task['agent_sessions'] is None  # the fake runner never reports a session
    report = json.loads(task['report'])
    assert task['state'] == 'review' and '## Frontend (Antigravity)\nfe' in report['report']


@pytest.mark.asyncio
async def test_frontend_issue_uses_the_task_prompt_with_image_rules(tmp_path):
    db, orchestrator, prompts = antigravity_setup(tmp_path, [classified('frontend')], [FRONTEND_REPORT], [],
                                                  claude_frontend=True,
                                                  task_instructions='VISUAL DESIGN AND IMAGERY: add images.')
    await orchestrator.process(issue())
    call = orchestrator.antigravity.run.await_args
    assert prompts == [] and 'YOU ARE THE IMPLEMENTER' in call.args[0] and 'VISUAL DESIGN AND IMAGERY' in call.args[0]
    assert '"images"' in json.dumps(call.kwargs['schema']) and db.get_task('TM-1')['state'] == 'review'


@pytest.mark.asyncio
async def test_antigravity_out_of_quota_hands_the_frontend_to_codex(tmp_path):
    db, orchestrator, prompts = antigravity_setup(tmp_path, [classified('frontend')], ['429 Resource exhausted'],
                                                  [COMPLETED], claude_frontend=True)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert len(prompts) == 1 and task['state'] == 'review'
    assert json.loads(task['agents'])['frontend']['agent'] == 'codex'
    assert any('Antigravity cannot code now' in event['message'] for event in db.events('TM-1'))


@pytest.mark.asyncio
async def test_backend_by_claude_code_fixes_review_findings_in_its_session(tmp_path):
    report = {**FRONTEND_REPORT, 'screenshots': []}
    db, orchestrator, prompts = setup(tmp_path, [report, CHANGES, report, APPROVE], [], claude_review=True,
                                      backend_agent='claude', claude_review_rounds=1)
    await orchestrator.process(issue())
    calls = orchestrator.claude.run.await_args_list
    task = db.get_task('TM-1')
    assert prompts == [] and task['state'] == 'review'
    assert calls[0].kwargs['write'] and 'YOU ARE THE IMPLEMENTER' in calls[0].args[0]
    assert 'A coding agent (Claude Code)' in calls[1].args[0]
    assert calls[2].kwargs['resume'] == json.loads(task['agent_sessions'])['backend'] == calls[0].kwargs['session_id']
    assert json.loads(task['agents'])['backend']['passes'] == 2


@pytest.mark.asyncio
async def test_plan_and_review_by_antigravity_stay_read_only(tmp_path):
    db, orchestrator, prompts = antigravity_setup(tmp_path, [], [PLAN, APPROVE], [COMPLETED], claude_plan=True,
                                                  claude_review=True, plan_agent='antigravity',
                                                  review_agent='antigravity', antigravity_model='gemini-4-hydrogen-high')
    await orchestrator.process(issue())
    calls = orchestrator.antigravity.run.await_args_list
    assert not any(call.kwargs.get('write') for call in calls) and calls[0].kwargs['model'] == 'gemini-4-hydrogen-high'
    assert 'IMPLEMENTATION PLAN' in prompts[0] and db.get_task('TM-1')['state'] == 'review'
    assert set(json.loads(db.get_task('TM-1')['agents'])) == {'plan', 'backend', 'review'}


def test_only_agents_some_enabled_job_needs_are_started():
    settings = SimpleNamespace(claude_plan=True, claude_review=False, claude_decide=False, claude_frontend=True,
                               plan_agent='claude', backend_agent='codex', frontend_agent='antigravity',
                               review_agent='claude', decide_agent='claude')
    assert agents_in_use(settings) == {'claude', 'codex', 'antigravity'}
    settings.claude_frontend = False
    assert agents_in_use(settings) == {'claude', 'codex'}
