import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.codex.app_server import CodexRunner
from jira_codex_agent.codex.models import CodexRunResult, RunOutcome
from jira_codex_agent.codex.report import REPORT_SCHEMA, parse_report
from jira_codex_agent.git.worktree import WorktreeManager
from jira_codex_agent.jira.models import JiraIssue, adf_to_text
from jira_codex_agent.scheduler.orchestrator import Orchestrator
from jira_codex_agent.storage.database import Database, TaskState


def issue(key='TM-1', links=(), status='To Do', labels=(), **extra):
    return JiraIssue.model_validate({'id': '1', 'key': key, 'fields': {
        'summary': 'Work', 'status': {'name': status}, 'labels': list(labels), 'issuelinks': list(links), **extra}})


def link(kind, side, key, category):
    return {'type': kind, side: {'key': key, 'fields': {'summary': 'Other', 'status': {
        'name': 'In Progress' if category != 'done' else 'Done', 'statusCategory': {'key': category}}}}}


BLOCKS = {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'}
DEPENDS = {'name': 'Dependency', 'inward': 'is depended on by', 'outward': 'depends on'}


def test_blockers_follow_unfinished_links_flags_and_labels():
    assert issue(links=[link(BLOCKS, 'inwardIssue', 'TM-9', 'indeterminate')]).blockers() == ['Blocked by TM-9 (In Progress)']
    assert issue(links=[link(BLOCKS, 'inwardIssue', 'TM-9', 'done')]).blockers() == []
    # "TM-1 blocks TM-9" does not block TM-1.
    assert issue(links=[link(BLOCKS, 'outwardIssue', 'TM-9', 'new')]).blockers() == []
    assert issue(links=[link(DEPENDS, 'outwardIssue', 'TM-8', 'new')]).blockers() == ['Blocked by TM-8 (In Progress)']
    assert issue(customfield_10021=[{'value': 'Impediment'}]).blockers('customfield_10021') == ['Flagged as impediment']
    assert issue(status='Blocked').blockers() == ['Status is Blocked']
    assert issue(labels=['Blocked']).blockers() == ['Labelled blocked']


def test_adf_keeps_lists_code_and_links_readable():
    document = {'type': 'doc', 'content': [
        {'type': 'heading', 'attrs': {'level': 2}, 'content': [{'type': 'text', 'text': 'AC'}]},
        {'type': 'bulletList', 'content': [{'type': 'listItem', 'content': [{'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'spec', 'marks': [{'type': 'link', 'attrs': {'href': 'https://x'}}]}]}]}]},
        {'type': 'codeBlock', 'attrs': {'language': 'sql'}, 'content': [{'type': 'text', 'text': 'select 1'}]}]}
    assert adf_to_text(document) == '## AC\n\n- [spec](https://x)\n\n```sql\nselect 1\n```'


def text(value, *marks):
    return {'type': 'text', 'text': value, 'marks': [{'type': mark} for mark in marks]}


def test_adf_markdown_keeps_emphasis_structure_and_tables():
    paragraph = lambda *nodes: {'type': 'paragraph', 'content': list(nodes)}
    item = lambda *nodes: {'type': 'listItem', 'content': list(nodes)}
    document = {'type': 'doc', 'content': [
        paragraph(text('AC1 ', 'strong'), text('— '), text('soạn', 'em'), text(' đề'), {'type': 'hardBreak'}, text('dòng 2')),
        {'type': 'rule'},
        {'type': 'bulletList', 'content': [item(paragraph(text('Given ', 'strong'), text('x')),
                                                {'type': 'orderedList', 'content': [item(paragraph(text('sub')))]})]},
        {'type': 'taskList', 'content': [{'type': 'taskItem', 'attrs': {'state': 'DONE'}, 'content': [text('done')]}]},
        {'type': 'panel', 'attrs': {'panelType': 'warning'}, 'content': [paragraph(text('careful'))]},
        {'type': 'table', 'content': [
            {'type': 'tableRow', 'content': [{'type': 'tableHeader', 'content': [paragraph(text('A'))]},
                                             {'type': 'tableHeader', 'content': [paragraph(text('B'))]}]},
            {'type': 'tableRow', 'content': [{'type': 'tableCell', 'content': [paragraph(text('1|2'))]},
                                             {'type': 'tableCell', 'content': [paragraph(text('3'))]}]}]}]}
    assert adf_to_text(document) == (
        '**AC1** — *soạn* đề\ndòng 2\n\n---\n\n- **Given** x\n  1. sub\n\n- [x] done\n\n> ⚠️ careful\n\n'
        '| A | B |\n| --- | --- |\n| 1\\|2 | 3 |')


def test_sync_blocks_and_unblocks_without_touching_finished_work(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-3', 'Skipped')
    db.update_task('TM-3', TaskState.SKIPPED)
    db.reconcile_queue([{'key': 'TM-1', 'summary': 'A', 'blocked_reason': 'Blocked by TM-9', 'description': 'Do A'},
                        {'key': 'TM-3', 'summary': 'Skipped'}])
    assert db.get_task('TM-1')['state'] == 'blocked'
    assert db.get_task('TM-1')['description'] == 'Do A'
    assert db.get_task('TM-3')['state'] == 'skipped'
    db.reconcile_queue([{'key': 'TM-1', 'summary': 'A'}])
    assert db.get_task('TM-1')['state'] == 'queued'
    assert db.get_task('TM-1')['blocked_reason'] is None
    db.reconcile_queue([])
    assert db.get_task('TM-1')['state'] == 'excluded'


def agent(db, **settings):
    values = dict(dry_run=False, jira_jql='project = TM', jira_poll_seconds=60, max_task_attempts=3,
                  retry_backoff_seconds=600, history_retention_days=30, approval_timeout_seconds=900,
                  task_instructions='Do it well.')
    values.update(settings)
    notify = []
    orchestrator = Orchestrator(SimpleNamespace(**values), db, None, None, None, None,
                                notify=lambda title, message: notify.append(title))
    orchestrator._respect_quota = AsyncMock(return_value=True)
    return orchestrator, notify


def run_result(message, outcome=RunOutcome.SUCCEEDED):
    return CodexRunResult(outcome=outcome, final_message=message, session_id='thread-1',
                          error=None if outcome == RunOutcome.SUCCEEDED else 'app-server disconnected')


@pytest.mark.parametrize('message,state', [
    (json.dumps({'status': 'COMPLETED', 'summary': 'Done', 'blockers': [], 'tests': [], 'branch': 'b', 'report': 'r'}), 'review'),
    (json.dumps({'status': 'BLOCKED_DEPENDENCY', 'summary': '', 'blockers': ['needs TM-9 API'], 'tests': [], 'branch': '', 'report': ''}), 'needs_human'),
    ('Status: NEEDS_HUMAN_REVIEW\nMissing permission rule', 'needs_human'),
    ('Implemented everything, trust me.', 'needs_human'),
])
def test_codex_report_decides_review_or_human_queue(tmp_path, message, state):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Work')
    orchestrator, notify = agent(db)
    orchestrator._finish('TM-1', run_result(message))
    task = db.get_task('TM-1')
    assert task['state'] == state
    assert notify
    if state == 'needs_human':
        assert task['error']
    if 'needs TM-9 API' in message:
        assert task['error'] == 'BLOCKED_DEPENDENCY: needs TM-9 API'


def test_failures_retry_with_backoff_then_give_up(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Work')
    orchestrator, notify = agent(db, max_task_attempts=2)
    orchestrator._finish('TM-1', run_result('', RunOutcome.FAILED))
    task = db.get_task('TM-1')
    assert task['state'] == 'interrupted' and task['attempts'] == 1
    assert task['session_id'] == 'thread-1'
    assert datetime.fromisoformat(task['next_attempt_at']) > datetime.now(timezone.utc) + timedelta(seconds=590)
    assert db.due_recovery() == []
    orchestrator._finish('TM-1', run_result('', RunOutcome.TIMED_OUT))
    assert db.get_task('TM-1')['state'] == 'failed'
    assert notify == ['TM-1: failed']


@pytest.mark.asyncio
async def test_waiting_retry_does_not_block_new_work(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Retry later')
    db.update_task('TM-1', TaskState.INTERRUPTED, next_attempt_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    orchestrator, _ = agent(db)
    orchestrator.jira = SimpleNamespace(search=AsyncMock(return_value=[issue('TM-2')]))
    orchestrator.process = AsyncMock()
    assert await orchestrator.run_cycle()
    assert orchestrator.process.await_args.args[0].key == 'TM-2'


@pytest.mark.asyncio
async def test_unreadable_recovery_issue_is_retried_later_not_forever_first(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Deleted in Jira')
    db.update_task('TM-1', TaskState.INTERRUPTED)
    orchestrator, _ = agent(db)
    orchestrator.jira = SimpleNamespace(get_issue=AsyncMock(side_effect=RuntimeError('404')))
    assert await orchestrator.run_cycle()
    assert db.get_task('TM-1')['attempts'] == 1
    assert db.due_recovery() == []


@pytest.mark.asyncio
async def test_discard_retry_resets_work_and_carries_note_into_prompt(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Wrong result')
    db.update_task('TM-1', TaskState.REVIEW, worktree='/w/tm-1', session_id='thread-1', report={'status': 'COMPLETED'})
    db.append_event('TM-1', 'command', 'old log')
    orchestrator, _ = agent(db)
    orchestrator.worktrees = SimpleNamespace(discard=AsyncMock(return_value=['feature/TM-1-x']))
    orchestrator.runner = SimpleNamespace(archive_thread=AsyncMock())
    assert await orchestrator.discard('TM-1', 'retry', 'Use the existing API client') == ['feature/TM-1-x']
    task = db.get_task('TM-1')
    assert (task['state'], task['worktree'], task['session_id'], task['report']) == ('queued', None, None, None)
    assert db.events('TM-1') == []
    orchestrator.runner.archive_thread.assert_awaited_with('thread-1')
    prompt = orchestrator._prompt(issue(), task)
    assert 'Use the existing API client' in prompt and 'output schema' in prompt
    await orchestrator.discard('TM-1', 'skip')
    db.reconcile_queue([{'key': 'TM-1', 'summary': 'Wrong result'}])
    assert db.get_task('TM-1')['state'] == 'skipped'
    await orchestrator.discard('TM-1', 'delete')
    assert db.get_task('TM-1') is None


@pytest.mark.asyncio
async def test_discard_aborts_the_running_task_first(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Running')
    orchestrator, _ = agent(db)
    started = asyncio.Event()

    async def run(*_, **__):
        started.set()
        await asyncio.sleep(60)

    orchestrator.runner = SimpleNamespace(run=run, archive_thread=AsyncMock())
    orchestrator.worktrees = SimpleNamespace(create=AsyncMock(return_value=tmp_path), discard=AsyncMock(return_value=[]))
    processing = asyncio.create_task(orchestrator.process(issue()))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(orchestrator.discard('TM-1', 'skip'), 5)
    await processing
    assert db.get_task('TM-1')['state'] == 'skipped'


def test_prune_expires_old_history_but_remembers_skips(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    for key, state in (('TM-1', 'done'), ('TM-2', 'excluded'), ('TM-3', 'skipped'), ('TM-4', 'review')):
        db.upsert_task(key, key)
        db.append_event(key, 'log', 'x')
        with db.connect() as connection:
            connection.execute("UPDATE tasks SET state=?, updated_at='2000-01-01' WHERE issue_key=?", (state, key))
    db.append_event('GONE-1', 'log', 'orphan')
    assert db.prune(30) == 4
    assert db.get_task('TM-2') is None
    assert db.get_task('TM-3')['state'] == 'skipped'
    assert len(db.events('TM-4')) == 1
    with db.connect() as connection:
        assert connection.execute('PRAGMA auto_vacuum').fetchone()[0] == 2


def test_event_history_is_capped_per_task(tmp_path):
    db = Database(tmp_path / 'state.db', max_events_per_task=50)
    db.initialize()
    for index in range(60):
        db.append_event('TM-1', 'log', str(index))
    events = db.events('TM-1')
    assert len(events) == 50 and events[-1]['message'] == '59'


def test_report_parser_accepts_json_with_surrounding_prose():
    report = parse_report('Here you go:\n{"status": "PARTIAL", "summary": "s", "blockers": ["b"], "tests": [], "branch": "", "report": ""}')
    assert report['status'] == 'PARTIAL'
    assert parse_report('{"status": "MAYBE"}') is None
    assert set(REPORT_SCHEMA['required']) == set(REPORT_SCHEMA['properties'])


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout


@pytest.mark.asyncio
async def test_discard_removes_worktree_and_renamed_issue_branch_only(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'develop')
    git(repo, '-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '--allow-empty', '-m', 'init')
    git(repo, 'branch', 'feature/TM-14-other')
    manager = WorktreeManager(repo, tmp_path / 'worktrees', 'develop')
    path = await manager.create('TM-1')
    git(path, 'branch', '-m', 'feature/TM-1-renamed')
    (path / 'wip.txt').write_text('uncommitted')
    assert await manager.discard('TM-1', path) == ['feature/TM-1-renamed']
    assert not path.exists()
    assert 'feature/TM-14-other' in git(repo, 'branch', '--list')
    assert await manager.discard('TM-1', path) == []


SERVER = '''#!/usr/bin/env python3
import json, sys
def send(m):
 print(json.dumps(m), flush=True)
for raw in sys.stdin:
 m = json.loads(raw)
 method = m.get('method')
 if method == 'initialize':
  send({'id':m['id'],'result':{}})
 elif method == 'thread/start':
  send({'id':m['id'],'result':{'thread':{'id':'thread-1'}}})
 elif method == 'turn/start':
  assert m['params']['outputSchema']['required']
  send({'id':m['id'],'result':{'turn':{'id':'turn-1'}}})
  send({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'threadId':'thread-1','itemId':'i'}})
 elif m.get('id') == 'approval-1':
  send({'method':'item/completed','params':{'item':{'type':'agentMessage','text':json.dumps(m['result'])}}})
  send({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
'''


@pytest.mark.asyncio
async def test_unanswered_approval_is_declined_after_timeout(tmp_path):
    path = tmp_path / 'fake-codex'
    path.write_text(SERVER)
    path.chmod(0o700)
    runner = CodexRunner(str(path), approval_timeout=1)
    events = []
    result = await asyncio.wait_for(runner.run('Work', tmp_path, on_event=events.append, output_schema=REPORT_SCHEMA), 5)
    assert json.loads(result.final_message) == {'decision': 'decline'}
    assert any(event['type'] == 'approval.timeout' for event in events)
    assert not runner.approvals
