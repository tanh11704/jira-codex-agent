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


PANEL_ICONS = {"info": "ℹ️", "note": "📝", "warning": "⚠️", "error": "⛔", "success": "✅", "tip": "💡"}


def adf_to_text(value: object | None) -> str:
    """Render Jira's Atlassian Document Format as Markdown (blocks separated by blank lines)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _block(value).strip()


def _children(node: dict) -> list:
    content = node.get("content")
    return content if isinstance(content, list) else []


def _wrap(text: str, marker: str, closing: str | None = None) -> str:
    # Markdown emphasis must hug the text: "**AC1 **" does not render, " **AC1** " does.
    core = text.strip()
    if not core:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    return f"{lead}{marker}{core}{closing if closing is not None else marker}{trail}"


def _inline(nodes: list) -> str:
    parts = []
    for node in nodes:
        if not isinstance(node, dict):
            parts.append(str(node))
            continue
        kind = node.get("type")
        if kind == "text":
            text = str(node.get("text", ""))
            marks = {mark.get("type"): mark.get("attrs") or {} for mark in node.get("marks") or []}
            if "code" in marks:
                text = _wrap(text, "`")
            if "strong" in marks:
                text = _wrap(text, "**")
            if "em" in marks:
                text = _wrap(text, "*")
            if "strike" in marks:
                text = _wrap(text, "~~")
            if href := marks.get("link", {}).get("href"):
                text = f"[{text}]({href})"
            parts.append(text)
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind in {"mention", "emoji", "inlineCard", "status", "date"}:
            attrs = node.get("attrs") or {}
            parts.append(str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or attrs.get("timestamp") or ""))
        else:
            parts.append(_inline(_children(node)))
    return "".join(parts)


def _list(node: dict, depth: int) -> str:
    lines = []
    ordered = node.get("type") == "orderedList"
    number = int((node.get("attrs") or {}).get("order", 1) or 1)
    indent = "  " * depth
    for item in _children(node):
        if node.get("type") == "taskList":
            marker = "- [x]" if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ]"
            lines.append(f"{indent}{marker} {_inline(_children(item)).strip()}")
            continue
        marker = f"{number}." if ordered else "-"
        number += 1
        first = True
        for child in _children(item):
            if child.get("type") in {"bulletList", "orderedList", "taskList"}:
                lines.append(_list(child, depth + 1))
                continue
            text = _block(child, depth + 1).strip().replace("\n", "\n" + indent + "  ")
            lines.append(f"{indent}{marker} {text}" if first else f"{indent}  {text}")
            first = False
        if first:
            lines.append(f"{indent}{marker}")
    return "\n".join(lines)


def _table(node: dict) -> str:
    rows = []
    header = False
    for index, row in enumerate(_children(node)):
        cells = [_inline(_flatten(cell)).strip().replace("|", "\\|").replace("\n", " ") for cell in _children(row)]
        if index == 0:
            header = any(cell.get("type") == "tableHeader" for cell in _children(row))
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    width = rows[0].count(" | ") + 1
    separator = "| " + " | ".join(["---"] * width) + " |"
    # Markdown needs a header row; without one an empty header keeps the data intact.
    return "\n".join([rows[0], separator, *rows[1:]] if header else ["| " + " | ".join([" "] * width) + " |", separator, *rows])


def _flatten(cell: dict) -> list:
    """Inline content of a table cell, paragraphs separated by spaces."""
    nodes = []
    for child in _children(cell):
        nodes.extend(_children(child) if child.get("type") == "paragraph" else [child])
        nodes.append({"type": "text", "text": " "})
    return nodes


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def _block(node: object, depth: int = 0) -> str:
    if isinstance(node, list):
        return "\n\n".join(part for part in (_block(child, depth) for child in node) if part.strip())
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "paragraph":
        return _inline(_children(node))
    if kind == "heading":
        return "#" * int(attrs.get("level", 2)) + " " + _inline(_children(node)).strip()
    if kind in {"bulletList", "orderedList", "taskList"}:
        return _list(node, 0)
    if kind == "codeBlock":
        return f"```{attrs.get('language') or ''}\n{_inline(_children(node))}\n```"
    if kind == "rule":
        return "---"
    if kind == "blockquote":
        return _quote(_block(_children(node)))
    if kind == "panel":
        icon = PANEL_ICONS.get(attrs.get("panelType", "info"), "ℹ️")
        return _quote(icon + " " + _block(_children(node)))
    if kind == "table":
        return _table(node)
    if kind in {"expand", "nestedExpand"}:
        return f"**{attrs.get('title') or 'Details'}**\n\n" + _block(_children(node))
    if kind in {"mediaSingle", "mediaGroup", "media"}:
        return "*[attachment]*"
    if kind in {"text", "hardBreak", "mention", "emoji", "inlineCard", "status", "date"}:
        return _inline([node])
    return _block(_children(node), depth)
