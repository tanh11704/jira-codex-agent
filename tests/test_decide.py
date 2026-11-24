import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.claude.prompts import decisions_report
from jira_codex_agent.storage.database import TaskState

from test_claude import COMPLETED, PLAN, setup
from test_unattended import issue

QUESTION = {'id': 'q2', 'question': 'Cho phép khóa những loại workspace nào?', 'context': 'WorkspaceType có ACCOUNT',
            'options': ['Chỉ PERSONAL và ORGANIZATION', 'Cả ACCOUNT']}
ASKS = json.dumps({'status': 'NEEDS_HUMAN_REVIEW', 'summary': 'Dừng lại', 'blockers': ['Thiếu quyết định'],
                   'questions': [QUESTION], 'tests': [], 'branch': 'b', 'report': 'Báo cáo'})
DECIDED = {'summary': 'Giữ phạm vi hẹp', 'decisions': [
    {'id': 'q2', 'question': 'ignored copy', 'answer': 'Chỉ PERSONAL và ORGANIZATION', 'rationale': 'SRS 4.2 chỉ nêu hai loại',
     'confidence': 'high', 'sources': ['docs/srs.md#4.2']}]}


@pytest.mark.asyncio
async def test_codex_questions_are_decided_and_summarized_for_review(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [DECIDED], [ASKS, COMPLETED], claude_decide=True, claude_decide_rounds=2)
    titles = []
    orchestrator.notify = lambda title, message: titles.append(title)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'review'
    assert len(prompts) == 2 and 'A: Chỉ PERSONAL và ORGANIZATION' in prompts[1]
    decision = json.loads(task['decisions'])[0]
    assert decision['by'] == 'agent' and decision['stage'] == 'report'
    assert decision['answers'][0]['question'] == QUESTION['question']  # the asked text, not Claude's copy
    report = json.loads(task['report'])['report']
    assert 'Quyết định do agent đưa ra' in report and 'SRS 4.2' in report and 'docs/srs.md#4.2' in report
    assert any('1 agent decision(s) to check' in title for title in titles)


@pytest.mark.asyncio
async def test_plan_questions_are_decided_before_codex_starts(tmp_path):
    plan = {**PLAN, 'area': 'backend', 'questions': ['Có audit lượt đọc không?']}
    decided = {'summary': 's', 'decisions': [{'id': 'p1', 'question': '', 'answer': 'Chỉ audit khi khóa/mở khóa',
                                              'rationale': 'r', 'confidence': 'medium', 'sources': []}]}
    db, orchestrator, prompts = setup(tmp_path, [plan, decided], [COMPLETED], claude_plan=True, claude_decide=True)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'review'
    assert 'HUMAN DECISIONS' in prompts[0] and 'Q: Có audit lượt đọc không?' in prompts[0]
    assert 'A: Chỉ audit khi khóa/mở khóa' in prompts[0]
    assert json.loads(task['decisions'])[0]['stage'] == 'plan'


@pytest.mark.asyncio
async def test_decision_rounds_are_bounded_and_failures_fall_back_to_human(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [DECIDED], [ASKS, ASKS], claude_decide=True, claude_decide_rounds=1)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'needs_human' and len(prompts) == 2
    assert 'Quyết định do agent đưa ra' in json.loads(task['report'])['report']

    (tmp_path / 'b').mkdir()
    db, orchestrator, prompts = setup(tmp_path / 'b', ['offline'], [ASKS], claude_decide=True)
    await orchestrator.process(issue())
    task = db.get_task('TM-1')
    assert task['state'] == 'needs_human' and task['decisions'] is None and len(prompts) == 1


@pytest.mark.asyncio
async def test_disabled_deciding_leaves_questions_to_the_human(tmp_path):
    db, orchestrator, prompts = setup(tmp_path, [], [ASKS])
    await orchestrator.process(issue())
    assert db.get_task('TM-1')['state'] == 'needs_human' and len(prompts) == 1


@pytest.mark.asyncio
async def test_waiting_task_can_be_handed_to_the_agent(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [DECIDED], [])
    db.update_task('TM-1', TaskState.NEEDS_HUMAN, report=json.loads(ASKS), worktree=str(tmp_path))
    orchestrator.jira = SimpleNamespace(get_issue=AsyncMock(return_value=issue()))
    orchestrator.request_decide('TM-1')
    with pytest.raises(ValueError):
        orchestrator.request_decide('TM-1')  # already deciding
    await asyncio.gather(*orchestrator._background)
    task = db.get_task('TM-1')
    assert json.loads(task['decisions'])[0]['stage'] == 'manual'
    assert task['state'] == 'resume_pending'
    assert db.due_recovery()[0]['issue_key'] == 'TM-1'


def test_report_section_skips_human_answers():
    assert decisions_report([{'by': 'human', 'answers': [{'question': 'Q', 'answer': 'A'}]}]) == ''


@pytest.mark.asyncio
async def test_waiting_backlog_is_decided_once_automatically(tmp_path):
    db, orchestrator, _ = setup(tmp_path, [DECIDED], [], claude_decide=True)
    db.update_task('TM-1', TaskState.NEEDS_HUMAN, report=json.loads(ASKS), worktree=str(tmp_path))
    orchestrator.jira = SimpleNamespace(get_issue=AsyncMock(return_value=issue()))
    orchestrator._decide_backlog()
    await asyncio.gather(*orchestrator._background)
    assert db.get_task('TM-1')['state'] == 'resume_pending'
    # Asked again after the agent already decided: it stays with the human.
    db.update_task('TM-1', TaskState.NEEDS_HUMAN)
    orchestrator._decide_backlog()
    assert not orchestrator._background and orchestrator.claude.run.await_count == 1
