import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow
from jira_codex_agent.main import ControlServer


@pytest.mark.asyncio
async def test_quota_endpoint_returns_live_daemon_thresholds(tmp_path):
    quota = SimpleNamespace(read=AsyncMock(return_value=QuotaSnapshot(
        primary=QuotaWindow(used_percent=15, window_minutes=300),
        secondary=QuotaWindow(used_percent=80, window_minutes=10080))))
    agent = SimpleNamespace(quota=quota, settings=SimpleNamespace(
        quota_remaining_threshold=20, quota_weekly_remaining_threshold=35))
    control = ControlServer(tmp_path / 'socket', None, agent)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"command":"quota"}\n')
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    await control._handle(reader, writer)
    response = json.loads(writer.write.call_args.args[0])
    assert response['fiveHourThreshold'] == 20
    assert response['weeklyThreshold'] == 35
    assert response['snapshot']['secondary']['used_percent'] == 80
    quota.read.assert_awaited_once()
