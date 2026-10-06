from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JiraUser(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_name: str | None = Field(default=None, alias="displayName")


class JiraStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "Unknown"


class JiraIssueFields(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    description: object | None = None
    status: JiraStatus | None = None
    assignee: JiraUser | None = None
    labels: list[str] = Field(default_factory=list)


class JiraIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    key: str
    fields: JiraIssueFields

    @property
    def summary(self) -> str:
        return self.fields.summary


def adf_to_text(value: object | None) -> str:
    """Flatten Jira's Atlassian Document Format without losing paragraph breaks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value)
    if not isinstance(value, dict):
        return str(value)
    text = str(value.get("text", ""))
    children = value.get("content", [])
    nested = "".join(adf_to_text(child) for child in children) if isinstance(children, list) else ""
    suffix = "\n" if value.get("type") in {"paragraph", "heading", "listItem"} else ""
    return text + nested + suffix
