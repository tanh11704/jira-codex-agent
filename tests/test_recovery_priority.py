from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.scheduler.orchestrator import Orchestrator
from jira_codex_agent.storage.database import Database, TaskState


def agent_for(db):
    settings = SimpleNamespace(dry_run=False, jira_jql='project = TM')
    jira = SimpleNamespace(search=AsyncMock(return_value=[]), get_issue=AsyncMock(return_value=SimpleNamespace(key='TM-1')))
    agent = Orchestrator(settings, db, jira, None, None, None)
    agent._respect_quota = AsyncMock(return_value=True)
    agent.process = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_restart_recovers_interrupted_before_any_new_task(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Interrupted task')
    db.update_task('TM-1', TaskState.INTERRUPTED, worktree='/original', session_id='session')
    db.upsert_task('TM-2', 'New task')
    agent = agent_for(db)
    assert await agent.run_cycle()
    agent.jira.search.assert_not_awaited()
    assert agent.process.await_args.kwargs['resume']['session_id'] == 'session'


@pytest.mark.asyncio
async def test_resume_arriving_during_quota_wait_prevents_new_task(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-1', 'Failed task')
    db.update_task('TM-1', TaskState.FAILED)
    agent = agent_for(db)
    agent.jira.search.return_value = [SimpleNamespace(key='TM-2', summary='New task')]

    async def quota():
        db.request_resume('TM-1')
        return True

    agent._respect_quota = quota
    assert await agent.run_cycle()
    agent.process.assert_not_awaited()
