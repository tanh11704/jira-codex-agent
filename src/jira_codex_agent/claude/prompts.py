from __future__ import annotations

AREAS = ("frontend", "backend", "fullstack")
AREA_PROPERTY = {"type": "string", "enum": list(AREAS),
                 "description": "frontend: only UI code (components, pages, styles, client state, calls to existing APIs); "
                                "backend: no UI change; fullstack: needs both server-side and UI changes"}
AREA_RULES = """Classify the issue by the code it needs changed:
- frontend: only UI code (components, pages, styles, client-side state, calls to endpoints that already exist).
- backend: no UI change (APIs, database, migrations, jobs, services, infrastructure, scripts).
- fullstack: needs both server-side and UI changes (e.g. a new endpoint plus the screen that uses it)."""

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["area", "summary", "steps", "files", "tests", "risks", "questions"],
    "properties": {
        "area": AREA_PROPERTY,
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

CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["area", "reason"],
    "properties": {"area": AREA_PROPERTY, "reason": {"type": "string", "description": "One sentence"}},
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


CONFIDENCES = ("high", "medium", "low")
RISKS = ("LOW", "MEDIUM", "HIGH")
DECIDE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "decisions"],
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences in Vietnamese on the overall direction chosen"},
        "decisions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "question", "answer", "rationale", "assumptions", "acceptance", "confidence", "risk",
                         "sources"],
            "properties": {
                "id": {"type": "string", "description": "The id of the question answered, exactly as given"},
                "question": {"type": "string", "description": "The question, copied as given"},
                "answer": {"type": "string",
                           "description": "The decision in Vietnamese, concrete enough to implement without asking again"},
                "rationale": {"type": "string", "description": "Why, in Vietnamese, citing what you found"},
                "assumptions": {"type": "array", "items": {"type": "string"},
                                "description": "Assumptions made where evidence was incomplete, in Vietnamese; empty when none"},
                "acceptance": {"type": "array", "items": {"type": "string"},
                               "description": "1-3 observable, testable outcomes that prove the decision is implemented, "
                                              "in Vietnamese"},
                "confidence": {"type": "string", "enum": list(CONFIDENCES),
                               "description": "high: specs or code settle it; medium: inferred from existing patterns; "
                                              "low: a judgement call"},
                "risk": {"type": "string", "enum": list(RISKS),
                         "description": "Impact if this decision turns out wrong: LOW cosmetic or easy to change; "
                                        "MEDIUM rework or a confusing workflow; HIGH security, grading or data integrity"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "Files the decision rests on (path, path#section or path:line); empty when none"},
            }}},
    },
}


def plan_prompt(task: str, instructions: str) -> str:
    return f"""You are the planning lead for an autonomous Jira implementation. Another coding agent with a much smaller context window will implement the issue from your plan in this Git worktree, so do the wide reading it cannot afford.

Explore the repository read-only: project rules (AGENTS.md, CLAUDE.md, RULES.md, CONTRIBUTING.md, README.md), relevant specs, contracts and the nearby code and tests. Do not modify any file.
Produce a concrete plan: ordered steps naming the exact files, classes and functions to change, the existing patterns to copy (with paths), contract/migration/UI impact, tests to add and the exact commands to verify.
For UI work, name the shared layout and control primitives to reuse (form grid, inputs, Button with hover/focus/disabled/pending states), how the screen lays out at 360 and 1280 px, its visual treatment (header, cards, icons from the portal's icon library, badges, and each image slot with its source, asset path and aspect ratio; existing repository assets first), and how to run the portal and capture screenshots for the visual check. Keep it focused on this issue. Note risks, and list missing product or security decisions under questions instead of inventing them. Write in English.
For full-stack issues, order the backend steps before the frontend steps: one agent implements the backend first, another the UI on top of it.

{AREA_RULES}

{task}

Rules the implementer will follow (for context; ignore its output/report format):
{instructions}
"""


def classify_prompt(task: str) -> str:
    return f"""Decide who should implement this Jira issue. Look at the repository read-only (layout, and the code the issue names) only as much as needed to tell where the change lands. Do not modify any file and do not plan the work.

{AREA_RULES}

{task}
"""


BACKEND_SCOPE = """
SCOPE: BACKEND ONLY
This is a full-stack issue. Implement only the server-side part (APIs, data, migrations, jobs, contracts) with its tests. Another agent implements the UI in this same worktree after you finish, so do not change frontend code; make the API it needs complete and documented. Report COMPLETED when the backend part is done and verified.
"""


IMPLEMENTER_SCOPE = """
YOU ARE THE IMPLEMENTER
You are coding this issue yourself in this Git worktree: edit files directly, run the checks and the visual verification, then report.
"""


def frontend_scope(backend: dict | None, backend_agent: str = 'Codex') -> str:
    if backend is None:
        return IMPLEMENTER_SCOPE
    tests = ', '.join(f"{t.get('command')} → {t.get('result')}" for t in backend.get('tests') or []) or 'none reported'
    return f"""
YOU ARE THE IMPLEMENTER: FRONTEND PART
This is a full-stack issue. Another agent ({backend_agent}) already implemented and verified the backend part; its changes are UNCOMMITTED in this worktree (see git status / git diff HEAD). Implement only the UI on top of them, edit files directly, run the checks and the visual verification, then report. Change backend code only when a defect blocks the UI, and explain it in the report.
Backend summary: {backend.get('summary', '')}
Backend tests: {tests}
"""


def merge_reports(backend: dict, frontend: dict, backend_agent: str = 'Codex',
                  frontend_agent: str = 'Claude Code') -> dict:
    """One handoff for a split issue: the frontend outcome decides, the backend notes stay readable."""
    return {**frontend, 'tests': (backend.get('tests') or []) + (frontend.get('tests') or []),
            'blockers': (backend.get('blockers') or []) + (frontend.get('blockers') or []),
            'report': f"## Backend ({backend_agent})\n{backend.get('report', '')}\n\n"
                      f"## Frontend ({frontend_agent})\n{frontend.get('report', '')}"}


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
Follow it unless the code proves a step wrong; then deviate and explain why in the report. Open questions answered under DECISIONS are settled; follow those answers. If an unanswered open question blocks the work, report NEEDS_HUMAN_REVIEW.
{plan}
"""


def review_prompt(task: str, plan: str | None, report: dict, coder: str = 'Codex') -> str:
    plan_text = f"\nThe agreed implementation plan:\n{plan}\n" if plan else ''
    screenshots = '\n'.join(f'- {path}' for path in report.get('screenshots') or []) or '- (none)'
    images = '\n'.join(f"- {item.get('placement')} [{item.get('source')}] {item.get('path')} {item.get('aspect_ratio')}: "
                       f"{item.get('url') or item.get('prompt')}" for item in report.get('images') or []) or '- (none)'
    return f"""You are the code reviewer for an autonomous Jira implementation. A coding agent ({coder}) left UNCOMMITTED changes in this Git worktree and reported them COMPLETED. Review them before a human does.

Inspect the change read-only: git status --short, git diff HEAD, and read every untracked file it lists. Read surrounding code and project rules (AGENTS.md, CLAUDE.md, RULES.md, CONTRIBUTING.md) as needed. Do not modify any file.
Check: acceptance criteria coverage, correctness and edge cases, authorization/tenant/data-safety, contract and migration drift, missing or weakened tests, rule violations, unrelated changes and leftovers. Be specific: give location, the problem and the fix.

When the diff touches UI (components, pages, styles), review it as a user would see it, not only as logic:
- Open every screenshot listed below with the Read tool and look for overlapping or clipped controls, horizontal overflow, misaligned labels/inputs/buttons, cramped or broken rows, text cut off or wrapping badly, and states that look unfinished. A visible layout defect is a major finding.
- Read the markup and classes for causes screenshots can miss: fixed widths on controls inside grid/flex cells, grid/flex children holding inputs without min-w-0, field rows that never stack on narrow widths, absolute positioning or negative margins that can collide, and raw elements bypassing shared primitives.
- Every clickable element needs cursor-pointer plus distinct hover, active and focus-visible states; disabled must look disabled; async actions need a pending state and visible success/error feedback. A main action without hover/pending feedback is a major finding.
- Imagery and visual richness: a learner- or parent-facing screen that is a plain wall of text or bare form, with no hierarchy, icons or cards, is a minor finding (major when the issue asks for a designed screen). An image URL that looks guessed or unverified, a web image without a license, a photo of an identifiable person, an image without a fixed aspect ratio or missing-file fallback, missing alt text, or an image slot in the code that is not listed in the report is a major finding. Check generation prompts are specific, in English, match the slot's aspect ratio and share one style.
- UI changes with no screenshots and no stated reason are a major finding; when the coder reported the visual check BLOCKED with a reason, note it as minor so the human checks it manually.

Severity: blocker/major must be fixed before human review; minor/nit are suggestions. Verdict is REQUEST_CHANGES only when at least one blocker or major finding exists, otherwise APPROVE. Write findings in English.

{task}
{plan_text}
{coder}'s report:
Summary: {report.get('summary', '')}
Tests: {', '.join(f"{t.get('command')} → {t.get('result')}" for t in report.get('tests') or []) or 'none reported'}
Screenshots (worktree-relative):
{screenshots}
Image slots:
{images}
{report.get('report', '')}
"""


def findings_text(review: dict) -> str:
    findings = '\n'.join(f"- [{item['severity']}] {item['location']}: {item['issue']} → {item['fix']}"
                         for item in review.get('findings') or [])
    return f"Review summary: {review.get('summary', '')}\nFindings:\n{findings}"


def fix_prompt(review: dict, contract: str) -> str:
    return f"""A code reviewer examined your uncommitted changes and requested changes.
{findings_text(review)}

Fix every valid blocker and major finding; apply minor ones when cheap and safe. If you disagree with a finding, leave the code and explain why in the report. Rerun the relevant checks, recapture the screenshots of any UI you changed, keep changes uncommitted and follow the same rules as before.

{contract}
"""


def decide_prompt(task: str, plan: str | None, questions: list[dict], instructions: str) -> str:
    """The product owner's instructions (configurable) followed by what it must decide (fixed)."""
    plan_text = f"\nThe implementation plan:\n{plan}\n" if plan else ''
    asked = '\n\n'.join(
        f"[{item.get('id')}] {item.get('question', '')}"
        + (f"\nContext: {item['context']}" if item.get('context') else '')
        + (''.join(f"\n  - Option: {option}" for option in item.get('options') or []))
        for item in questions)
    return f"""{instructions.strip()}

Do not modify any file. Decide every question below; answer each one exactly once, keeping its id.

{task}
{plan_text}
QUESTIONS TO DECIDE
{asked}
"""


def decision_lines(item: dict) -> str:
    """One answered question as the coder sees it: the answer plus the outcomes that prove it is done."""
    text = f"- Q: {item['question']}\n  A: {item['answer']}"
    if item.get('acceptance'):
        text += '\n  Acceptance: ' + '; '.join(item['acceptance'])
    return text


def decisions_prompt(answers: list[dict], contract: str) -> str:
    lines = '\n'.join(decision_lines(item) for item in answers)
    return f"""The product owner answered your questions. These answers are authoritative: implement accordingly in this same worktree, finish the remaining work, run the relevant checks and report again. Do not ask these questions again; ask only decisions that are genuinely new.
{lines}

{contract}
"""


def decisions_report(decisions: list[dict]) -> str:
    """The handoff section listing every decision the agent made, for the human to confirm; risky ones first."""
    labels = {'high': 'cao', 'medium': 'trung bình', 'low': 'thấp'}
    risks = {'HIGH': 'cao', 'MEDIUM': 'trung bình', 'LOW': 'thấp'}
    stages = {'plan': 'lúc lập kế hoạch', 'report': 'khi agent code hỏi', 'manual': 'theo yêu cầu của bạn'}
    items = [(decision, item) for decision in decisions if decision.get('by') == 'agent'
             for item in decision.get('answers') or []]
    if not items:
        return ''

    def flagged(item: dict) -> bool:
        return item.get('risk') == 'HIGH' or item.get('confidence') == 'low'

    items.sort(key=lambda pair: not flagged(pair[1]))  # stable: otherwise in the order decided
    lines = []
    for number, (decision, item) in enumerate(items, 1):
        meta = f"Độ tin cậy: {labels.get(item.get('confidence'), item.get('confidence') or '?')}"
        if item.get('risk'):
            meta += f" · Rủi ro: {risks.get(item['risk'], item['risk'])}"
        meta += f" · {stages.get(decision.get('stage'), decision.get('stage') or '')}"
        if item.get('sources'):
            meta += ' · Nguồn: ' + ', '.join(item['sources'])
        text = f"{number}. {'⚠️ ' if flagged(item) else ''}**{item['question']}**\n   - Quyết định: {item['answer']}"
        if item.get('rationale'):
            text += f"\n   - Lý do: {item['rationale']}"
        if item.get('assumptions'):
            text += '\n   - Giả định: ' + '; '.join(item['assumptions'])
        if item.get('acceptance'):
            text += '\n   - Nghiệm thu: ' + '; '.join(item['acceptance'])
        lines.append(text + f"\n   - {meta}")
    return ('## Quyết định do agent đưa ra (cần bạn xác nhận)\n'
            'Agent đã tự quyết các điểm dưới đây để hoàn thành task. Mục ⚠️ (rủi ro cao hoặc độ tin cậy thấp) được xếp '
            'lên đầu, nên xem trước. Nếu sai, chạy lại task kèm ghi chú.\n\n'
            + '\n'.join(lines))
