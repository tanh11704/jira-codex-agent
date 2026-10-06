from __future__ import annotations

import httpx

from .models import JiraIssue


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, *, timeout: float = 30) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(email, api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout,
        )

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, jql: str, *, max_results: int = 50) -> list[JiraIssue]:
        issues: list[JiraIssue] = []
        seen_tokens: set[str] = set()
        token: str | None = None
        while True:
            params = {
                "jql": jql,
                "maxResults": max_results,
                "fields": "summary,description,status,assignee,labels",
            }
            if token:
                params["nextPageToken"] = token
            response = await self._client.get("/rest/api/3/search/jql", params=params)
            response.raise_for_status()
            payload = response.json()
            issues.extend(JiraIssue.model_validate(issue) for issue in payload.get("issues", []))
            token = payload.get("nextPageToken")
            if payload.get("isLast") is True or not token:
                return issues
            if token in seen_tokens:
                raise RuntimeError("Jira returned a repeated pagination token; queue was not synchronized")
            seen_tokens.add(token)

    async def get_issue(self, key: str) -> JiraIssue:
        response = await self._client.get(
            f"/rest/api/3/issue/{key}",
            params={"fields": "summary,description,status,assignee,labels"},
        )
        response.raise_for_status()
        return JiraIssue.model_validate(response.json())
