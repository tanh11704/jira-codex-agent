import httpx
import pytest
import respx

from jira_codex_agent.jira.client import JiraClient


@pytest.mark.asyncio
@respx.mock
async def test_search_parses_jira_issues() -> None:
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={"issues": [{"id": "1", "key": "DATN-1", "fields": {"summary": "Build it"}}]},
        )
    )
    async with JiraClient("https://example.atlassian.net", "me@example.com", "secret") as jira:
        issues = await jira.search("assignee = currentUser()")
    assert route.called
    assert issues[0].key == "DATN-1"
    assert issues[0].summary == "Build it"
