from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow

# `/usage` prints e.g. "Current session: 11% used · resets Oct 19 at 1:09pm (Asia/Saigon)".
WINDOWS = {'session': 300, 'week (all models)': 10080}
LINE = re.compile(r'^Current (session|week \(all models\)):\s*(\d+)% used(?:\s*·\s*resets\s+(.+?))?\s*$', re.MULTILINE)
RESET = re.compile(r'^(?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:,? (?P<year>\d{4}))? at )?'
                   r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)(?:\s*\((?P<zone>[^)]+)\))?$', re.IGNORECASE)
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def parse_reset(text: str, now: datetime) -> datetime | None:
    match = RESET.match(text.strip())
    if not match:
        return None
    try:
        zone = ZoneInfo(match['zone']) if match['zone'] else now.astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        zone = now.astimezone().tzinfo
    local = now.astimezone(zone)
    hour = int(match['hour']) % 12 + (12 if match['half'].lower() == 'pm' else 0)
    minute = int(match['minute'] or 0)
    if match['month']:
        month = MONTHS.index(match['month'].title()) + 1
        year = int(match['year'] or local.year)
        reset = local.replace(year=year, month=month, day=int(match['day']), hour=hour, minute=minute,
                              second=0, microsecond=0)
        if not match['year'] and reset < local - timedelta(days=1):
            reset = reset.replace(year=year + 1)  # "Jan 2" read on Dec 30
    else:
        reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset < local:
            reset += timedelta(days=1)
    return reset.astimezone(timezone.utc)


def parse_usage(text: str, now: datetime | None = None) -> QuotaSnapshot:
    now = now or datetime.now(timezone.utc)
    windows = {}
    for name, used, reset in LINE.findall(text):
        windows[WINDOWS[name]] = QuotaWindow(used_percent=min(100, int(used)), window_minutes=WINDOWS[name],
                                             resets_at=parse_reset(reset, now) if reset else None)
    if not windows:
        first = text.strip().splitlines()[0] if text.strip() else 'empty output'
        return QuotaSnapshot(error=f'Could not read Claude usage: {first[:200]}')
    return QuotaSnapshot(primary=windows.get(300), secondary=windows.get(10080))


class ClaudeQuotaReader:
    """Read Claude subscription limits through the CLI's `/usage` command; it makes no model request."""

    def __init__(self, command: str = 'claude', timeout: float = 30, cwd: Path | None = None) -> None:
        self.command, self.timeout, self.cwd = command, timeout, cwd
        self.cached: QuotaSnapshot | None = None
        self.checked_at: datetime | None = None
        self._checked_monotonic = 0.0
        self._lock = asyncio.Lock()

    async def read(self, *, force: bool = False) -> QuotaSnapshot:
        async with self._lock:
            if not force and self.cached is not None and time.monotonic() - self._checked_monotonic < 60:
                return self.cached
            self.cached = await self._read_live()
            self.checked_at = datetime.now(timezone.utc)
            self._checked_monotonic = time.monotonic()
            return self.cached

    async def _read_live(self) -> QuotaSnapshot:
        try:
            process = await asyncio.create_subprocess_exec(
                self.command, '-p', '/usage', '--output-format', 'json', '--no-session-persistence',
                cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return QuotaSnapshot(error=f'Could not start {self.command}: {exc}')
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return QuotaSnapshot(error='Claude usage check timed out')
        try:
            result = json.loads(stdout)
        except ValueError:
            return QuotaSnapshot(error=(stderr or stdout).decode(errors='replace').strip()[:300] or 'No output')
        if result.get('is_error'):
            return QuotaSnapshot(error=str(result.get('result') or result.get('subtype')))
        return parse_usage(result.get('result') or '')
