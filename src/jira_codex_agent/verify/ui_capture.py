"""Screenshots and layout checks of changed UI, taken by the daemon instead of the coding agent.

The agent only declares which screens to render and the API responses they need ("ui_captures" in its report). The
daemon renders them from the built portal in a headless browser outside the agent's sandbox, checks overflow and
overlapping controls, and hands the screenshots to the reviewer. The agent spends no tokens starting servers, driving
a browser or looking at images, and the capture never fails on sandbox limits."""
from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from pydantic import BaseModel

from .checks import run_check

SCRIPT = Path(__file__).with_name('ui_capture.mjs')
OUT_DIR = '.ui-evidence/auto'
WIDTHS = [360, 1280]
PLAYWRIGHT = 'playwright-core@^1'

# What a coding agent declares per screen; required keys keep the schema strict (empty values when unused).
CAPTURE_SCHEMA = {
    "type": "array",
    "description": ("Screens the automation renders and checks after your change; one item per changed screen and "
                    "state (empty, filled, error). Empty when no UI changed"),
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "dist", "path", "local_storage", "mocks", "focus"],
        "properties": {
            "name": {"type": "string", "description": "kebab-case file name, e.g. teacher-curriculum-empty"},
            "dist": {"type": "string", "description": "Worktree-relative built portal directory, e.g. apps/teacher-web/dist"},
            "path": {"type": "string", "description": "Route to open, e.g. /curriculum?tab=subjects"},
            "local_storage": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["key", "value"],
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}},
            "mocks": {"type": "array", "description": "API responses the screen needs, session/refresh included", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["method", "path", "status", "content_type", "body"],
                "properties": {"method": {"type": "string", "description": "GET, POST… or * for any"},
                               "path": {"type": "string", "description": "URL path glob, e.g. /api/v1/subjects*"},
                               "status": {"type": "integer"},
                               "content_type": {"type": "string", "description": "Empty means application/json"},
                               "body": {"type": "string", "description": "Response body, usually JSON text"}}}},
            "focus": {"type": "string", "description": "CSS selector of the main action to capture focused; empty for none"},
        },
    },
}


class CaptureResult(BaseModel):
    ok: bool
    blocked: str = ''  # the capture could not run at all; not the agent's fault
    output: str = ''  # problems for the agent to fix, one per line
    screenshots: list[str] = []  # worktree-relative PNG paths
    seconds: float = 0


def capture_section() -> str:
    return f"""
UI SCREENSHOTS (taken by the automation, not by you)
This replaces any instruction above to run the app, open a browser or capture and inspect screenshots yourself: do none of that. For every screen and state you change, add an item to "ui_captures" in your report instead: the built portal directory, the route, localStorage entries and the API mocks the screen needs (session/refresh included), so it renders with realistic Vietnamese data. Make sure the portal builds (its build is part of the local checks). After the checks pass, the automation renders each item at {' and '.join(f'{w}px' for w in WIDTHS)} plus the "focus" selector focused, fails on horizontal overflow, overlapping controls and page errors, and sends any problem back to you. Leave "screenshots" empty; the automation fills it for the reviewer.
"""


async def ensure_playwright(data_dir: Path, timeout: int) -> tuple[Path | None, str]:
    """playwright-core installed once into the daemon's data directory; the browser is the installed Google Chrome."""
    root = data_dir / 'ui-capture'
    if (root / 'node_modules' / 'playwright-core' / 'package.json').exists():
        return root, ''
    root.mkdir(parents=True, exist_ok=True)
    # Through the login shell (run_check), so node/npm from mise or Homebrew are found even under launchd.
    result = await run_check(f'npm install --no-audit --no-fund --prefix {shlex.quote(str(root))} {PLAYWRIGHT}',
                             root, timeout)
    if not result.ok:
        return None, f'Could not install {PLAYWRIGHT}: {result.output[-500:]}'
    return root, ''


async def capture(worktree: Path, captures: list[dict], data_dir: Path, timeout: int) -> CaptureResult:
    playwright, error = await ensure_playwright(data_dir, timeout)
    if playwright is None:
        return CaptureResult(ok=False, blocked=error)
    out = worktree / OUT_DIR
    shutil.rmtree(out, ignore_errors=True)  # screenshots of the previous round would mislead the reviewer
    spec = data_dir / 'ui-capture' / f'spec-{worktree.name}.json'
    spec.write_text(json.dumps({'worktree': str(worktree.resolve()), 'widths': WIDTHS, 'captures': captures},
                               ensure_ascii=False))
    command = ' '.join(shlex.quote(str(part)) for part in ('node', SCRIPT, spec, out, playwright))
    result = await run_check(command, worktree, timeout)
    text = result.output if not result.ok else ''
    saved = []
    for path in sorted(out.glob('*.png')) if out.is_dir() else []:
        saved.append(str(path.relative_to(worktree)))
    if result.ok:
        return CaptureResult(ok=True, screenshots=saved, seconds=result.seconds)
    if result.exit_code == 1:
        problems = '\n'.join(line for line in text.splitlines() if line.strip() and not line.startswith('SAVED '))
        return CaptureResult(ok=False, output=problems, screenshots=saved, seconds=result.seconds)
    return CaptureResult(ok=False, blocked=f'UI capture could not run (exit {result.exit_code}): {text[-500:]}',
                         screenshots=saved, seconds=result.seconds)
