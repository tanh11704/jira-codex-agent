from __future__ import annotations

import json
import re

STATUSES = ("COMPLETED", "PARTIAL", "BLOCKED_BRANCH", "BLOCKED_DEPENDENCY", "NEEDS_HUMAN_REVIEW")

# Passed to turn/start as outputSchema so the final message is machine-readable.
REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "blockers", "questions", "tests", "branch", "report"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "summary": {"type": "string", "description": "One or two sentences on the resulting behaviour"},
        "blockers": {"type": "array", "items": {"type": "string"},
                     "description": "Why the work is not COMPLETED; empty when COMPLETED"},
        "questions": {"type": "array", "description": "Decisions only a human can make; empty when none", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "question", "context", "options"],
            "properties": {"id": {"type": "string", "description": "Short stable identifier, e.g. q1"},
                           "question": {"type": "string", "description": "One decision, answerable on its own"},
                           "context": {"type": "string", "description": "Why it matters and what you found"},
                           "options": {"type": "array", "items": {"type": "string"},
                                       "description": "Concrete choices you would accept; empty for free text"}}}},
        "tests": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["command", "result"],
            "properties": {"command": {"type": "string"},
                           "result": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]}}}},
        "branch": {"type": "string"},
        "report": {"type": "string", "description": "The full human handoff report in Vietnamese"},
    },
}


def output_contract(approval_timeout: int) -> str:
    wait = (f"Approval requests not answered within {max(1, approval_timeout // 60)} minutes are declined automatically. "
            if approval_timeout else "")
    return f"""UNATTENDED MODE AND OUTPUT CONTRACT (enforced by the automation)
No human is watching this run. {wait}Prefer actions that work inside the workspace sandbox. If an essential action is declined or unavailable, do not loop or retry it; stop and report, naming the declined request in "blockers". A human can allow it and resume this session.
Your final message must be JSON matching the provided output schema. Put the complete human report (in Vietnamese, following the FINAL REPORT format) in "report".
When a product, security or scope decision is missing, ask it in "questions": one decision per item, written in Vietnamese, with the context you found and the concrete options you would accept. A human answers them later and you resume this same session and worktree, so leave the work in a resumable state.
"status" is COMPLETED only when the implementation and relevant executable checks are complete. Use BLOCKED_DEPENDENCY when the work needs another unfinished issue, BLOCKED_BRANCH for branch/worktree problems, NEEDS_HUMAN_REVIEW for missing product/security decisions, otherwise PARTIAL. Any status other than COMPLETED sends the task to the human queue and the automation moves on to the next issue."""


def parse_report(message: str) -> dict | None:
    """Read Codex's final report; tolerate prose around JSON and the older text format."""
    candidates = [message]
    if (start := message.find("{")) >= 0 and (end := message.rfind("}")) > start:
        candidates.append(message[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("status") in STATUSES:
            data.setdefault("questions", [])  # reports written before questions existed
            return data
    match = re.search(r"Status:\s*\**\s*([A-Z_]+)", message)
    if match and match.group(1) in STATUSES:
        return {"status": match.group(1), "summary": "", "blockers": [], "questions": [], "tests": [], "branch": "",
                "report": message}
    return None
