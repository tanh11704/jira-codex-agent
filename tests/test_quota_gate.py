from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow
from jira_codex_agent.scheduler.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_weekly_limit_blocks_even_with_available_five_hour_quota():
    settings = SimpleNamespace(quota_remaining_threshold=30, quota_weekly_remaining_threshold=30)
    reset = datetime.now(timezone.utc) + timedelta(days=2)
    blocked = QuotaSnapshot(
        primary=QuotaWindow(used_percent=10, window_minutes=300),
        secondary=QuotaWindow(used_percent=70, window_minutes=10080, resets_at=reset),
    )
    recovered = QuotaSnapshot(primary=QuotaWindow(used_percent=10, window_minutes=300),
                              secondary=QuotaWindow(used_percent=10, window_minutes=10080))
    quota = SimpleNamespace(read=AsyncMock(side_effect=[blocked, recovered]))
    agent = Orchestrator(settings, None, None, None, quota, None)
    waits = []

    async def simulated_timeout(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        raise TimeoutError

    with patch('jira_codex_agent.scheduler.orchestrator.asyncio.wait_for', side_effect=simulated_timeout):
        assert await agent._respect_quota()
    assert waits[0] > 172000
    assert quota.read.await_count == 2


@pytest.mark.asyncio
async def test_unknown_quota_defers_task():
    agent = Orchestrator(SimpleNamespace(), None, None, None,
                         SimpleNamespace(read=AsyncMock(return_value=QuotaSnapshot(error='unavailable'))), None)
    assert await agent._respect_quota() is False
