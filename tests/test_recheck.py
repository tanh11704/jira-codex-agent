import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.storage.database import TaskState
from jira_codex_agent.verify.checks import CheckResult

from test_claude import setup
from test_unattended import issue

TIMED_OUT = {'status': 'PARTIAL', 'summary': 'Done', 'blockers': ['make check timed out in TaxonomyPage'],
             'questions': [], 'tests': [], 'branch': 'b', 'report': 'r'}
BLOCKED = {'status': 'BLOCKED_DEPENDENCY', 'summary': 'Needs TM-103', 'questions': [], 'tests': [], 'branch': 'b',
           'report': 'r', 'blockers': ['Thiếu triển khai TM-103 trong worktree']}


def waiting(db, tmp_path, report):
    db.update_task('TM-1', TaskState.NEEDS_HUMAN, worktree=str(tmp_path), session_id='thread-1', report=report)


@pytest.mark.asyncio
async def test_recheck_reruns_checks_without_an_agent_turn(tmp_path, monkeypatch):
    db, orchestrator, prompts = setup(tmp_path, [], [], verify_commands=['make check'])
    waiting(db, tmp_path, TIMED_OUT)
    calls = []

    async def check(command, cwd, timeout):
        calls.append(command)
        return CheckResult(command=command, ok=True, exit_code=0)
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', check)
    orchestrator.request_recheck('TM-1')
    assert db.get_task('TM-1')['state'] == 'resume_pending' and db.get_task('TM-1')['recheck'] == 1
    await orchestrator.process(issue(), resume=db.get_task('TM-1'))
    task = db.get_task('TM-1')
    report = json.loads(task['report'])
    assert prompts == [] and calls == ['make check']  # no agent turn at all
    assert task['state'] == 'review' and task['recheck'] == 0 and report['status'] == 'COMPLETED'
    assert 'TaxonomyPage' in report['report']  # the earlier blocker stays readable


@pytest.mark.asyncio
async def test_recheck_failure_wakes_the_agent_in_its_session(tmp_path, monkeypatch):
    done = json.dumps(TIMED_OUT | {'status': 'COMPLETED', 'blockers': []})
    db, orchestrator, prompts = setup(tmp_path, [], [done], verify_commands=['make check'], verify_fix_rounds=1)
    waiting(db, tmp_path, TIMED_OUT)
    outcomes = iter([False, True])

    async def check(command, cwd, timeout):
        ok = next(outcomes)
        return CheckResult(command=command, ok=ok, exit_code=0 if ok else 1, output='' if ok else 'Error: x',
                           signature=[] if ok else ['error: x'])
    monkeypatch.setattr('jira_codex_agent.scheduler.orchestrator.run_check', check)
    orchestrator.request_recheck('TM-1')
    await orchestrator.process(issue(), resume=db.get_task('TM-1'))
    assert len(prompts) == 1 and 'make check → FAIL' in prompts[0]
    assert db.get_task('TM-1')['state'] == 'review'


def test_recheck_refuses_tasks_it_cannot_settle(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [], [])
    waiting(db, tmp_path, TIMED_OUT | {'questions': [{'id': 'q1', 'question': 'Q', 'context': '', 'options': []}]})
    with pytest.raises(ValueError, match='questions'):
        orchestrator.request_recheck('TM-1')
    waiting(db, tmp_path, BLOCKED)
    with pytest.raises(ValueError, match='PARTIAL'):
        orchestrator.request_recheck('TM-1')


def jira_with(statuses):
    async def get_issue(key):
        category = {'key': statuses.get(key, 'new')}
        return SimpleNamespace(key=key, blockers=lambda flag=None: [],
                               fields=SimpleNamespace(status=SimpleNamespace(done=category['key'] == 'done')))
    return SimpleNamespace(get_issue=get_issue)


@pytest.mark.asyncio
async def test_dependency_done_in_jira_resumes_the_blocked_task_once(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [], [])
    waiting(db, tmp_path, BLOCKED)
    orchestrator.worktrees = SimpleNamespace(update_base=AsyncMock(return_value=None))
    orchestrator.jira = jira_with({})
    await orchestrator._resume_unblocked()
    assert db.get_task('TM-1')['state'] == 'needs_human'  # TM-103 not done yet

    orchestrator.jira = jira_with({'TM-103': 'done'})
    orchestrator._last_unblock_check = None
    await orchestrator._resume_unblocked()
    task = db.get_task('TM-1')
    assert task['state'] == 'resume_pending' and json.loads(task['auto_resumed']) == ['TM-103']
    orchestrator.worktrees.update_base.assert_awaited_once()

    db.update_task('TM-1', TaskState.NEEDS_HUMAN)  # blocked again on the same issue: stays with the human
    orchestrator._last_unblock_check = None
    await orchestrator._resume_unblocked()
    assert db.get_task('TM-1')['state'] == 'needs_human'


@pytest.mark.asyncio
async def test_worktree_that_cannot_move_to_the_new_base_waits_for_a_human(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [], [])
    waiting(db, tmp_path, BLOCKED)
    orchestrator.worktrees = SimpleNamespace(update_base=AsyncMock(return_value='1 file(s) changed both'))
    orchestrator.jira = jira_with({'TM-103': 'done'})
    await orchestrator._resume_unblocked()
    assert db.get_task('TM-1')['state'] == 'needs_human'


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.mark.asyncio
async def test_update_base_keeps_uncommitted_work_and_refuses_overlaps(tmp_path):
    origin = tmp_path / 'origin'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'develop')
    git(origin, 'config', 'user.email', 't@e.st')
    git(origin, 'config', 'user.name', 't')
    (origin / 'a.txt').write_text('a\n')
    git(origin, 'add', '.')
    git(origin, 'commit', '-qm', 'base')
    repo = tmp_path / 'repo'
    git(tmp_path, 'clone', '-q', '-b', 'develop', str(origin), str(repo))
    manager = WorktreeManager(repo, tmp_path / 'worktrees', 'develop')
    worktree = await manager.create('TM-104')
    (worktree / 'mine.txt').write_text('task work\n')  # untracked
    (worktree / 'a.txt').write_text('a\nchanged by task\n')  # modified

    (origin / 'dep.txt').write_text('merged dependency\n')  # TM-103 lands on develop
    git(origin, 'add', '.')
    git(origin, 'commit', '-qm', 'TM-103')
    assert await manager.update_base(worktree) is None
    assert (worktree / 'dep.txt').exists() and (worktree / 'mine.txt').read_text() == 'task work\n'
    assert 'changed by task' in (worktree / 'a.txt').read_text()

    (origin / 'a.txt').write_text('a\nconflicting upstream\n')
    git(origin, 'commit', '-qam', 'touch a')
    problem = await manager.update_base(worktree)
    assert problem and 'a.txt' in problem and 'changed by task' in (worktree / 'a.txt').read_text()
