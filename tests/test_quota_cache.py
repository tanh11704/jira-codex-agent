import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow
from jira_codex_agent.codex.quota import QuotaReader
from jira_codex_agent.scheduler.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_cached_quota_does_not_launch_repeated_reads():
    reader = QuotaReader()
    reader._read_live = AsyncMock(return_value=QuotaSnapshot(primary=QuotaWindow(used_percent=10)))
    await reader.read()
    await reader.read()
    assert reader._read_live.await_count == 1
    await reader.read(force=True)
    assert reader._read_live.await_count == 2


@pytest.mark.asyncio
async def test_check_now_wakes_scheduler_before_old_reset():
    blocked = QuotaSnapshot(primary=QuotaWindow(used_percent=80, window_minutes=300,
        resets_at=datetime.now(timezone.utc) + timedelta(hours=2)))
    recovered = QuotaSnapshot(primary=QuotaWindow(used_percent=0, window_minutes=300))
    quota = SimpleNamespace(read=AsyncMock(side_effect=[blocked, recovered]))
    agent = Orchestrator(SimpleNamespace(quota_remaining_threshold=30), None, None, None, quota, None)
    task = asyncio.create_task(agent._respect_quota())
    await asyncio.sleep(0)
    agent.wake_event.set()
    assert await asyncio.wait_for(task, 1)
    assert quota.read.await_count == 2
