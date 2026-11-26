import subprocess
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.config import Settings
from jira_codex_agent.verify.checks import CheckResult, failure_signature, run_checks, trim_output

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
    """Fake check runs, one per round (one command each): True passes, a string is the failing command."""
    calls = []

    async def fake(command, cwd, timeout):
        calls.append(command)
        outcome = outcomes[len(calls) - 1]
        if outcome is True:
            return CheckResult(command=command, ok=True, seconds=1, exit_code=0)
        return CheckResult(command=outcome, ok=False, seconds=1, output='Error: x', exit_code=1, signature=['error: x'])
    return calls, fake


@pytest.mark.asyncio
async def test_codex_gets_trimmed_failure_and_passing_checks_land_in_report(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [COMPLETED, COMPLETED], verify_commands=['npm test'],
                                      verify_fix_rounds=1)
    calls, fake = checks(orchestrator, 'npm test', True)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert 'LOCAL CHECKS' in prompts[0] and 'npm test' in prompts[0]
    assert 'npm test → FAIL' in prompts[1] and 'Error: x' in prompts[1]
    assert task['state'] == 'review' and json.loads(task['report'])['tests'] == [{'command': 'npm test', 'result': 'PASS'}]


@pytest.mark.asyncio
async def test_check_still_failing_after_rounds_goes_to_human(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [COMPLETED], verify_commands=['npm run build'], verify_fix_rounds=0)
    _, fake = checks(orchestrator, 'npm run build')
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert len(prompts) == 1 and task['state'] == 'needs_human' and 'Local check failed: npm run build' in task['error']


@pytest.mark.asyncio
async def test_claude_frontend_fixes_failed_check_in_its_session(tmp_path, monkeypatch):
    db, orchestrator, _ = setup(tmp_path, [classified('frontend'), FRONTEND_REPORT, FRONTEND_REPORT], [],
                                claude_frontend=True, frontend_agent='claude', verify_commands=['npm run build'], verify_fix_rounds=1)
    _, fake = checks(orchestrator, 'npm run build', True)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    fix = orchestrator.claude.run.await_args_list[2]
    assert fix.kwargs['resume'] == json.loads(task['agent_sessions'])['frontend'] and 'npm run build → FAIL' in fix.args[0]
    assert task['state'] == 'review'


PARTIAL_SANDBOX = json.dumps({'status': 'PARTIAL', 'summary': 'Done', 'blockers': ['Docker blocked in sandbox'],
                              'questions': [], 'sandbox_blocked_only': True, 'tests': [{'command': './mvnw verify', 'result': 'BLOCKED'}],
                              'branch': 'b', 'report': 'r'})


@pytest.mark.asyncio
async def test_sandbox_blocked_partial_is_verified_here_and_promoted(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [PARTIAL_SANDBOX], verify_commands=['./mvnw verify'])
    _, fake = checks(orchestrator, True)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', fake)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    report = json.loads(task['report'])
    assert task['state'] == 'review' and report['status'] == 'COMPLETED' and report['blockers'] == []
    assert report['tests'] == [{'command': './mvnw verify', 'result': 'PASS'}]
    assert 'Docker' in prompts[0] and 'sandbox_blocked_only' in prompts[0]


@pytest.mark.asyncio
async def test_other_partial_reports_are_not_verified(tmp_path, monkeypatch):
    unfinished = json.loads(PARTIAL_SANDBOX) | {'sandbox_blocked_only': False}
    db, orchestrator, _ = setup(tmp_path, [], [json.dumps(unfinished)], verify_commands=['./mvnw verify'])
    calls, fake = checks(orchestrator)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', fake)
    await orchestrator.process(issue())
    assert calls == [] and db.get_task('TM-1')['state'] == 'needs_human'


def test_failure_signature_ignores_paths_numbers_and_noise():
    a = failure_signature('/w/tm-1/src/a.test.tsx FAIL renders (12ms)\nok line\n✗ 25 tests failed', '/w/tm-1')
    b = failure_signature('/w/_baseline/src/a.test.tsx FAIL renders (40ms)\n✗ 27 tests failed', '/w/_baseline')
    assert a == b == sorted(['/src/a.test.tsx fail renders (#ms)', '✗ # tests failed'])


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True)


def baseline_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.email', 't@e.st')
    git(repo, 'config', 'user.name', 't')
    # A check that already fails on the base commit.
    (repo / 'check.sh').write_text('echo "Error: legacy test"\nexit 1\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    task = tmp_path / 'worktrees' / 'tm-1'
    git(repo, 'worktree', 'add', '-q', '-b', 'codex/tm-1', str(task))
    return repo, task


@pytest.mark.asyncio
@pytest.mark.parametrize('blamed', [False, True])
async def test_failures_already_on_base_are_not_blamed(tmp_path, blamed):
    repo, task = baseline_repo(tmp_path)
    if blamed:  # the task's own change adds a new error to the same check
        (task / 'check.sh').write_text('echo "Error: new bug"\necho "Error: legacy test"\nexit 1\n')
    (tmp_path / 'data').mkdir()
    db, orchestrator, _ = setup(tmp_path / 'data', [], [COMPLETED], verify_commands=['sh check.sh'], verify_fix_rounds=0,
                                repository=repo, worktree_root=tmp_path / 'worktrees', data_dir=tmp_path / 'data',
                                base_branch='main')
    orchestrator.worktrees = SimpleNamespace(create=AsyncMock(return_value=task))
    await orchestrator.process(issue())
    state = db.get_task('TM-1')
    report = json.loads(state['report'])
    assert (tmp_path / 'worktrees' / '_baseline' / 'check.sh').exists()
    if blamed:
        assert state['state'] == 'needs_human' and report['tests'][0]['result'] == 'FAIL'
    else:
        assert state['state'] == 'review' and report['tests'][0]['result'] == 'PRE-EXISTING'
        assert 'Check lỗi sẵn có trên base' in report['report']
        assert json.loads((tmp_path / 'data' / 'baseline-checks.json').read_text())
