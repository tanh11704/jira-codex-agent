from __future__ import annotations

import logging

import httpx

from .models import JiraIssue

log = logging.getLogger(__name__)

FIELDS = "summary,description,status,assignee,labels,issuetype,priority,issuelinks"


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, *, timeout: float = 30) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(email, api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout,
        )
        self._flag_field: str | None = None
        self._flag_field_loaded = False

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def flag_field(self) -> str | None:
        """ID of the site's "Flagged" (impediment) custom field, if it has one."""
        if not self._flag_field_loaded:
            try:
                response = await self._client.get("/rest/api/3/field")
                response.raise_for_status()
                self._flag_field = next((field["id"] for field in response.json()
                                         if field.get("name") == "Flagged"), None)
            except Exception:  # optional feature; never block queue sync on it
                log.warning("could not discover Jira Flagged field; flag detection disabled")
            self._flag_field_loaded = True
        return self._flag_field

    async def _fields(self) -> str:
        flag = await self.flag_field()
        return FIELDS + (f",{flag}" if flag else "")

    async def search(self, jql: str, *, max_results: int = 50) -> list[JiraIssue]:
        issues: list[JiraIssue] = []
        seen_tokens: set[str] = set()
        token: str | None = None
        fields = await self._fields()
        while True:
            params = {
                "jql": jql,
                "maxResults": max_results,
                "fields": fields,
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
            params={"fields": await self._fields()},
        )
        response.raise_for_status()
        return JiraIssue.model_validate(response.json())
