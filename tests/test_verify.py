import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.config import Settings
from jira_codex_agent.verify.checks import run_checks, trim_output

from test_claude import COMPLETED, FRONTEND_REPORT, classified, setup
from test_unattended import issue


def test_trim_keeps_errors_and_tail_but_drops_noise():
    log = '\n'.join(f'compiled module {i}' for i in range(2000)) + '\nsrc/a.ts(3,1): error TS2304: Cannot find name x\n'
    trimmed = trim_output(log + '\n'.join(f'done {i}' for i in range(10)))
    assert 'error TS2304' in trimmed and 'done 9' in trimmed and 'compiled module 10\n' not in trimmed
    assert len(trimmed) < 3000


@pytest.mark.asyncio
async def test_run_checks_stops_at_first_failure(tmp_path):
    results = await run_checks(['true', 'echo boom >&2; echo "Error: broken"; exit 3', 'touch ran'], tmp_path, 30)
    assert [r.ok for r in results] == [True, False] and results[1].exit_code == 3
    assert 'Error: broken' in results[1].output and not (tmp_path / 'ran').exists()


def test_commands_setting_skips_blank_and_comment_lines():
    text = 'npm ci\n\n# lint later\nnpm run build\n'
    settings = Settings.model_construct(verify_commands_base64=base64.b64encode(text.encode()).decode())
    assert settings.verify_commands == ['npm ci', 'npm run build']


def checks(orchestrator, *outcomes):
    """Fake check runs: each outcome is True (all pass) or a failing command name."""
    calls = []

    async def fake(commands, cwd, timeout, on_result=None):
        calls.append(commands)
        outcome = outcomes[len(calls) - 1]
        if outcome is True:
            return [SimpleNamespace(command=c, ok=True, seconds=1, output='', exit_code=0) for c in commands]
        return [SimpleNamespace(command=outcome, ok=False, seconds=1, output='Error: x', exit_code=1)]
    return calls, fake


@pytest.mark.asyncio
async def test_codex_gets_trimmed_failure_and_passing_checks_land_in_report(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [COMPLETED, COMPLETED], verify_commands=['npm test'],
                                      verify_fix_rounds=1)
    calls, fake = checks(orchestrator, 'npm test', True)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_checks', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert 'LOCAL CHECKS' in prompts[0] and 'npm test' in prompts[0]
    assert 'npm test → FAIL' in prompts[1] and 'Error: x' in prompts[1]
    assert task['state'] == 'review' and json.loads(task['report'])['tests'] == [{'command': 'npm test', 'result': 'PASS'}]


@pytest.mark.asyncio
async def test_check_still_failing_after_rounds_goes_to_human(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [COMPLETED], verify_commands=['npm run build'], verify_fix_rounds=0)
    _, fake = checks(orchestrator, 'npm run build')
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_checks', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert len(prompts) == 1 and task['state'] == 'needs_human' and 'Local check failed: npm run build' in task['error']


@pytest.mark.asyncio
async def test_claude_frontend_fixes_failed_check_in_its_session(tmp_path, monkeypatch):
    db, orchestrator, _ = setup(tmp_path, [classified('frontend'), FRONTEND_REPORT, FRONTEND_REPORT], [],
                                claude_frontend=True, verify_commands=['npm run build'], verify_fix_rounds=1)
    _, fake = checks(orchestrator, 'npm run build', True)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_checks', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    fix = orchestrator.claude.run.await_args_list[2]
    assert fix.kwargs['resume'] == task['claude_session_id'] and 'npm run build → FAIL' in fix.args[0]
    assert task['state'] == 'review'
