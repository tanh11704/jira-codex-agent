from __future__ import annotations

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "steps", "files", "tests", "risks", "questions"],
    "properties": {
        "summary": {"type": "string", "description": "What the change achieves, in one or two sentences"},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "Ordered implementation steps naming concrete files, symbols and patterns to reuse"},
        "files": {"type": "array", "items": {"type": "string"}, "description": "Files to create or change"},
        "tests": {"type": "array", "items": {"type": "string"},
                  "description": "Tests to add and exact commands to run"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"},
                      "description": "Missing product or security decisions; empty when none"},
    },
}

SEVERITIES = ("blocker", "major", "minor", "nit")
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "location", "issue", "fix"],
            "properties": {
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "location": {"type": "string", "description": "path:line, or the area affected"},
                "issue": {"type": "string"},
                "fix": {"type": "string"},
            }}},
    },
}


def plan_prompt(task: str, instructions: str) -> str:
    return f"""You are the planning lead for an autonomous Jira implementation. Another coding agent (Codex) with a much smaller context window will implement the issue from your plan in this Git worktree, so do the wide reading it cannot afford.

Explore the repository read-only: project rules (AGENTS.md, CLAUDE.md, RULES.md, CONTRIBUTING.md, README.md), relevant specs, contracts and the nearby code and tests. Do not modify any file.
Produce a concrete plan: ordered steps naming the exact files, classes and functions to change, the existing patterns to copy (with paths), contract/migration/UI impact, tests to add and the exact commands to verify. Keep it focused on this issue. Note risks, and list missing product or security decisions under questions instead of inventing them. Write in English.

{task}

Rules the implementer will follow (for context; ignore its output/report format):
{instructions}
"""


def plan_markdown(plan: dict) -> str:
    def bullets(items: list[str], ordered: bool = False) -> str:
        return '\n'.join(f"{f'{i}.' if ordered else '-'} {item}" for i, item in enumerate(items, 1)) or '- (none)'
    return (f"{plan.get('summary', '').strip()}\n\n### Steps\n{bullets(plan.get('steps') or [], True)}"
            f"\n\n### Files\n{bullets(plan.get('files') or [])}\n\n### Tests\n{bullets(plan.get('tests') or [])}"
            f"\n\n### Risks\n{bullets(plan.get('risks') or [])}"
            f"\n\n### Open questions\n{bullets(plan.get('questions') or [])}")


def plan_section(plan: str) -> str:
    return f"""
IMPLEMENTATION PLAN (prepared by a planning reviewer who read the codebase)
Follow it unless the code proves a step wrong; then deviate and explain why in the report. If its open questions block the work, report NEEDS_HUMAN_REVIEW.
{plan}
"""


def review_prompt(task: str, plan: str | None, report: dict) -> str:
    plan_text = f"\nThe agreed implementation plan:\n{plan}\n" if plan else ''
    return f"""You are the code reviewer for an autonomous Jira implementation. A coding agent (Codex) left UNCOMMITTED changes in this Git worktree and reported them COMPLETED. Review them before a human does.

Inspect the change read-only: git status --short, git diff HEAD, and read every untracked file it lists. Read surrounding code and project rules (AGENTS.md, CLAUDE.md, RULES.md, CONTRIBUTING.md) as needed. Do not modify any file.
Check: acceptance criteria coverage, correctness and edge cases, authorization/tenant/data-safety, contract and migration drift, missing or weakened tests, rule violations, unrelated changes and leftovers. Be specific: give location, the problem and the fix.
Severity: blocker/major must be fixed before human review; minor/nit are suggestions. Verdict is REQUEST_CHANGES only when at least one blocker or major finding exists, otherwise APPROVE. Write findings in English.

{task}
{plan_text}
Codex's report:
Summary: {report.get('summary', '')}
Tests: {', '.join(f"{t.get('command')} → {t.get('result')}" for t in report.get('tests') or []) or 'none reported'}
{report.get('report', '')}
"""


def findings_text(review: dict) -> str:
    findings = '\n'.join(f"- [{item['severity']}] {item['location']}: {item['issue']} → {item['fix']}"
                         for item in review.get('findings') or [])
    return f"Review summary: {review.get('summary', '')}\nFindings:\n{findings}"


def fix_prompt(review: dict, contract: str) -> str:
    return f"""A code reviewer examined your uncommitted changes and requested changes.
{findings_text(review)}

Fix every valid blocker and major finding; apply minor ones when cheap and safe. If you disagree with a finding, leave the code and explain why in the report. Rerun the relevant checks, keep changes uncommitted and follow the same rules as before.

{contract}
"""
