import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.codex.app_server import CodexRunner, approval_matches
from jira_codex_agent.codex.models import CodexRunResult, RunOutcome
from jira_codex_agent.codex.report import REPORT_SCHEMA, parse_report
from jira_codex_agent.storage.database import Database, TaskState

from test_unattended import SERVER, agent, issue

QUESTION = {'id': 'q1', 'question': 'Giáo viên có được xoá đề đã giao?', 'context': 'SRS không nói rõ',
            'options': ['Có', 'Không']}
NEEDS_HUMAN = json.dumps({'status': 'NEEDS_HUMAN_REVIEW', 'summary': 's', 'blockers': [], 'questions': [QUESTION],
                          'tests': [], 'branch': 'b', 'report': 'r'})
COMPLETED = json.dumps({'status': 'COMPLETED', 'summary': 'Done', 'blockers': [], 'questions': [], 'tests': [],
                        'branch': 'b', 'report': 'r'})
DECLINED = {'kind': 'item/commandExecution/requestApproval', 'command': 'docker compose up -d', 'cwd': '/w', 'reason': None}


def database(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Work')
    return db


def test_questions_send_task_to_human_queue_with_count(tmp_path):
    db = database(tmp_path)
    orchestrator, _ = agent(db)
    orchestrator._finish('TM-1', CodexRunResult(outcome=RunOutcome.SUCCEEDED, final_message=NEEDS_HUMAN, session_id='t'))
    task = db.get_task('TM-1')
    assert task['state'] == 'needs_human' and task['error'] == 'NEEDS_HUMAN_REVIEW: 1 question to answer'
    assert json.loads(task['report'])['questions'][0]['id'] == 'q1'
    assert parse_report('{"status": "PARTIAL", "summary": "", "blockers": [], "tests": [], "branch": "", "report": ""}')['questions'] == []
    assert 'questions' in REPORT_SCHEMA['required']


def test_answer_records_decisions_allows_requests_and_resumes(tmp_path):
    db = database(tmp_path)
    db.update_task('TM-1', TaskState.NEEDS_HUMAN, declined_approvals=[DECLINED, {**DECLINED, 'command': 'curl x'}])
    orchestrator, _ = agent(db)
    with pytest.raises(ValueError):
        orchestrator.answer('TM-1', [{'id': 'q1', 'question': 'Q', 'answer': '  '}], None, [])
    orchestrator.answer('TM-1', [{'id': 'q1', 'question': QUESTION['question'], 'answer': 'Không'}], 'Giữ soft delete', [0])
    task = db.get_task('TM-1')
    assert task['state'] == 'resume_pending'
    decision = json.loads(task['decisions'])[0]
    assert decision['answers'] == [{'question': QUESTION['question'], 'answer': 'Không'}] and decision['note'] == 'Giữ soft delete'
    assert json.loads(task['allowed_approvals']) == [DECLINED]
    assert [item['command'] for item in json.loads(task['declined_approvals'])] == ['curl x']
    assert orchestrator.wake_event.is_set()
    db.update_task('TM-1', TaskState.REVIEW)
    with pytest.raises(ValueError):
        orchestrator.answer('TM-1', [], 'late', [])


@pytest.mark.asyncio
async def test_resume_carries_decisions_blockers_and_allowed_requests(tmp_path):
    db = database(tmp_path)
    db.update_task('TM-1', TaskState.NEEDS_HUMAN, worktree=str(tmp_path), session_id='thread-1',
                   report=json.loads(NEEDS_HUMAN) | {'blockers': ['Thiếu quy tắc xoá']}, declined_approvals=[DECLINED])
    orchestrator, _ = agent(db)
    orchestrator.answer('TM-1', [{'id': 'q1', 'question': QUESTION['question'], 'answer': 'Không'}], None, [0])
    calls = []

    async def run(prompt, cwd, **kwargs):
        calls.append((prompt, kwargs))
        return CodexRunResult(outcome=RunOutcome.SUCCEEDED, final_message=COMPLETED, session_id='thread-1')

    orchestrator.runner = SimpleNamespace(run=run)
    await orchestrator.process(issue(), resume=db.get_task('TM-1'))
    prompt, kwargs = calls[0]
    assert kwargs['session_id'] == 'thread-1' and kwargs['preapproved'] == [DECLINED]
    assert 'ended NEEDS_HUMAN_REVIEW: Thiếu quy tắc xoá' in prompt
    assert 'HUMAN DECISIONS' in prompt and 'A: Không' in prompt
    assert '- docker compose up -d' in prompt
    assert db.get_task('TM-1')['state'] == 'review'


@pytest.mark.asyncio
async def test_timed_out_approval_is_kept_for_later(tmp_path):
    db = database(tmp_path)
    orchestrator, _ = agent(db)

    async def run(prompt, cwd, *, on_event, **_):
        for _ in range(2):  # the same request twice is kept once
            on_event({'type': 'approval.timeout', 'message': 'declined', 'approval': {**DECLINED, 'id': 'x', 'details': '{}'}})
        return CodexRunResult(outcome=RunOutcome.SUCCEEDED, final_message=NEEDS_HUMAN, session_id='thread-1')

    orchestrator.runner = SimpleNamespace(run=run)
    orchestrator.worktrees = SimpleNamespace(create=AsyncMock(return_value=tmp_path))
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'needs_human' and json.loads(task['declined_approvals']) == [DECLINED]


@pytest.mark.asyncio
async def test_preapproved_request_is_accepted_without_waiting(tmp_path):
    path = tmp_path / 'fake-codex'
    path.write_text(SERVER)
    path.chmod(0o700)
    runner = CodexRunner(str(path), approval_timeout=0)  # would otherwise wait forever
    events = []
    allowed = [{'kind': 'item/commandExecution/requestApproval', 'command': None}]
    result = await asyncio.wait_for(runner.run('Work', tmp_path, on_event=events.append, output_schema=REPORT_SCHEMA,
                                               preapproved=allowed), 5)
    assert json.loads(result.final_message) == {'decision': 'accept'}
    assert any(event['type'] == 'approval.preapproved' for event in events)
    assert not approval_matches({'kind': 'item/commandExecution/requestApproval', 'command': 'rm -rf /'},
                                {'kind': 'item/commandExecution/requestApproval', 'command': 'ls'})
