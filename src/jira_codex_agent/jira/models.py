from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JiraUser(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_name: str | None = Field(default=None, alias="displayName")


class JiraStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "Unknown"


class JiraNamed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class JiraIssueFields(BaseModel):
    # Extra fields are kept so a site-specific "Flagged" custom field can be read.
    model_config = ConfigDict(extra="allow")
    summary: str
    description: object | None = None
    status: JiraStatus | None = None
    assignee: JiraUser | None = None
    labels: list[str] = Field(default_factory=list)
    issuetype: JiraNamed | None = None
    priority: JiraNamed | None = None
    issuelinks: list[dict] = Field(default_factory=list)


class JiraIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    key: str
    fields: JiraIssueFields

    @property
    def summary(self) -> str:
        return self.fields.summary

    def blockers(self, flag_field: str | None = None) -> list[str]:
        """Reasons this issue cannot be worked on yet; empty when it is ready."""
        reasons = []
        for link in self.fields.issuelinks:
            kind = link.get("type") or {}
            if "inwardIssue" in link and (
                kind.get("name", "").lower() == "blocks" or "blocked by" in kind.get("inward", "").lower()
            ):
                other = link["inwardIssue"]
            elif "outwardIssue" in link and "depends on" in kind.get("outward", "").lower():
                other = link["outwardIssue"]
            else:
                continue
            status = (other.get("fields") or {}).get("status") or {}
            if (status.get("statusCategory") or {}).get("key") != "done":
                reasons.append(f"Blocked by {other.get('key', '?')} ({status.get('name', 'unknown status')})")
        status_name = self.fields.status.name if self.fields.status else ""
        if "block" in status_name.lower():
            reasons.append(f"Status is {status_name}")
        if flag_field and (self.fields.model_extra or {}).get(flag_field):
            reasons.append("Flagged as impediment")
        if any(label.lower() == "blocked" for label in self.fields.labels):
            reasons.append("Labelled blocked")
        return reasons


def adf_to_text(value: object | None) -> str:
    """Render Jira's Atlassian Document Format as lightweight Markdown."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value)
    if not isinstance(value, dict):
        return str(value)
    kind = value.get("type")
    children = value.get("content") if isinstance(value.get("content"), list) else []
    if kind == "text":
        text = str(value.get("text", ""))
        for mark in value.get("marks") or []:
            if mark.get("type") == "code":
                text = f"`{text}`"
            elif mark.get("type") == "link" and (href := (mark.get("attrs") or {}).get("href")):
                text = f"[{text}]({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind in {"mention", "emoji", "inlineCard"}:
        attrs = value.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or "")
    if kind == "codeBlock":
        return f"```{(value.get('attrs') or {}).get('language') or ''}\n{adf_to_text(children)}\n```\n"
    if kind == "heading":
        return "#" * int((value.get("attrs") or {}).get("level", 2)) + " " + adf_to_text(children) + "\n"
    if kind in {"bulletList", "orderedList"}:
        lines = []
        for index, item in enumerate(children, 1):
            marker = f"{index}." if kind == "orderedList" else "-"
            body = adf_to_text(item).strip().replace("\n", "\n  ")
            lines.append(f"{marker} {body}")
        return "\n".join(lines) + "\n"
    if kind == "tableRow":
        return "| " + " | ".join(adf_to_text(cell).strip().replace("\n", " ") for cell in children) + " |\n"
    nested = adf_to_text(children)
    if kind in {"paragraph", "blockquote", "panel", "table", "listItem", "rule"}:
        return nested + "\n"
    return nested
