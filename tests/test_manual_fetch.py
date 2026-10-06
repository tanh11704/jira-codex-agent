from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.scheduler.orchestrator import Orchestrator
from jira_codex_agent.storage.database import Database, TaskState


@pytest.mark.asyncio
async def test_manual_sync_while_paused_updates_queue_without_running_codex(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.set_paused(True)
    db.upsert_task('HAN-1', 'Outside filter')
    jira = SimpleNamespace(search=AsyncMock(return_value=[SimpleNamespace(key='TM-1', summary='Sprint task')]))
    runner = SimpleNamespace(run=AsyncMock())
    agent = Orchestrator(SimpleNamespace(jira_jql='project = TM'), db, jira, runner, None, None)
    assert len(await agent.sync_jira()) == 1
    assert db.is_paused()
    assert [row['issue_key'] for row in db.list_tasks(TaskState.QUEUED)] == ['TM-1']
    runner.run.assert_not_awaited()
