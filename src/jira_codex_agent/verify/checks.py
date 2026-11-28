from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from pathlib import Path

from pydantic import BaseModel

# What an agent sees of a failed check: the lines that name the problem plus the end of the log, never the whole log.
MAX_FEEDBACK_CHARS = 6000
TAIL_LINES = 40
ERROR_LINE = re.compile(r'error|fail|exception|traceback|cannot|unable|not found|undefined|expected|assert|✗|✘|×',
                        re.IGNORECASE)
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
MAX_SIGNATURE_LINES = 2000
MAX_KEPT_BYTES = 4_000_000


class CheckResult(BaseModel):
    command: str
    ok: bool
    exit_code: int | None = None
    seconds: float = 0
    output: str = ''  # trimmed log, only kept for failures
    signature: list[str] = []  # normalized error lines of the full log, to compare a failure with the base commit


TIMEOUT_LINE = re.compile(r'timed out|timeout|time-out', re.IGNORECASE)


def timed_out(result: CheckResult) -> bool:
    """Whether a failed check reports per-test timeouts (e.g. Vitest's "Test timed out in 5000ms").

    A check the automation itself killed for running past verify_timeout_seconds has no exit code and is not counted:
    rerunning it would only take as long again."""
    if result.ok or result.exit_code is None:
        return False
    return any(TIMEOUT_LINE.search(line) for line in [*result.signature, result.output])


def killed_by_timeout(result: CheckResult) -> bool:
    """Whether the automation itself killed the check for running past verify_timeout_seconds."""
    return not result.ok and result.exit_code is None and result.output.startswith('Timed out after')


def failure_signature(text: str, root: Path | str | None = None) -> list[str]:
    """Error lines of a log made comparable across worktrees and runs: no paths, numbers, timings or colours."""
    if root:
        text = text.replace(str(root), '')
    lines = set()
    for line in ANSI.sub('', text).replace('\r', '\n').splitlines():
        if not ERROR_LINE.search(line):
            continue
        line = re.sub(r'\s+', ' ', re.sub(r'\d+', '#', line)).strip().lower()
        if line:
            lines.add(line[:300])
    return sorted(lines)[:MAX_SIGNATURE_LINES]


def trim_output(text: str, limit: int = MAX_FEEDBACK_CHARS) -> str:
    """Keep error lines (with one line of context) and the log tail, deduplicated and capped."""
    lines = ANSI.sub('', text).replace('\r', '\n').splitlines()
    keep: set[int] = set(range(max(0, len(lines) - TAIL_LINES), len(lines)))
    for index, line in enumerate(lines):
        if ERROR_LINE.search(line):
            keep.update({index - 1, index, index + 1, index + 2})
    picked, previous = [], -2
    for index in sorted(i for i in keep if 0 <= i < len(lines)):
        if not lines[index].strip():
            continue
        if index != previous + 1 and picked:
            picked.append('…')
        picked.append(lines[index].rstrip()[:400])
        previous = index
    result = '\n'.join(picked)
    if len(result) > limit:
        # The first errors usually explain the rest; keep the head of the selection and the very end.
        result = result[:limit * 2 // 3] + '\n…\n' + result[-limit // 3:]
    return result


async def run_check(command: str, cwd: Path, timeout: int) -> CheckResult:
    """Run one shell command in the worktree through a login shell, so PATH matches the user's terminal."""
    started = time.monotonic()
    shell = os.environ.get('SHELL') or '/bin/zsh'
    process = await asyncio.create_subprocess_exec(
        shell, '-lc', command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, env={**os.environ, 'CI': '1', 'FORCE_COLOR': '0', 'NO_COLOR': '1'})
    chunks: list[bytes] = []

    async def drain() -> None:
        # Read as it arrives so a check killed for running too long still leaves its log behind.
        while chunk := await process.stdout.read(65536):
            chunks.append(chunk)
            if sum(map(len, chunks)) > MAX_KEPT_BYTES * 2:
                chunks[:] = [b''.join(chunks)[-MAX_KEPT_BYTES:]]

    try:
        await asyncio.wait_for(drain(), timeout)
        await process.wait()
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        partial = b''.join(chunks).decode(errors='replace')
        return CheckResult(command=command, ok=False, seconds=time.monotonic() - started,
                           output=f'Timed out after {timeout}s (killed by the automation). Last output before the kill:\n'
                                  + trim_output(partial), signature=failure_signature(partial, cwd))
    output = b''.join(chunks)
    ok = process.returncode == 0
    text = '' if ok else output.decode(errors='replace')
    return CheckResult(command=command, ok=ok, exit_code=process.returncode, seconds=round(time.monotonic() - started, 1),
                       output=trim_output(text) if text else '', signature=failure_signature(text, cwd) if text else [])


async def run_checks(commands: list[str], cwd: Path, timeout: int, on_result=None) -> list[CheckResult]:
    """Run commands in order and stop at the first failure; later checks rarely help while the build is broken."""
    results = []
    for command in commands:
        result = await run_check(command, cwd, timeout)
        results.append(result)
        if on_result:
            on_result(result)
        if not result.ok:
            break
    return results


def failure_feedback(results: list[CheckResult], skipped: list[str]) -> str:
    failed = results[-1]
    passed = ''.join(f'- {r.command} → PASS\n' for r in results[:-1])
    not_run = ''.join(f'- {command} → not run\n' for command in skipped)
    code = f'exit {failed.exit_code}' if failed.exit_code is not None else 'no exit code'
    return f"""{passed}- {failed.command} → FAIL ({code})
{not_run}
Relevant output (trimmed):
```
{failed.output}
```"""


def checks_section(commands: list[str]) -> str:
    listed = '\n'.join(f'- {command}' for command in commands)
    return f"""
LOCAL CHECKS (run by the automation, not by you)
After you report COMPLETED, the automation runs these commands in this worktree on the developer's machine, in order:
{listed}
Do not run them, or any other full build or full test suite, yourself: their logs fill your context and waste tokens. While coding, run only narrow and fast checks when you need them (one test file, one type-check, one lint target). If a command fails you get its trimmed output in this session to fix; when all pass they are added to your report. In "tests", list only the checks you ran yourself.
These commands run outside your sandbox, with Docker, Testcontainers and the network available. When a check needs those and your sandbox blocks it, do not request escalation or retry it: mark it BLOCKED in "tests" and, if the implementation is otherwise complete, report COMPLETED (or set "sandbox_blocked_only" to true) so the automation verifies it here. A failure that already exists on the base commit is detected automatically and not blamed on you.
"""


def check_fix_prompt(feedback: str, contract: str) -> str:
    return f"""The automation ran the local checks on your COMPLETED change and one failed:
{feedback}

Fix the cause (not the check), keep changes uncommitted and follow the same rules as before. Do not rerun the full build or test suite yourself; the automation reruns all checks after you report. If the failure is unrelated to your change or cannot be fixed in this worktree, report PARTIAL and explain it in "blockers".

{contract}
"""
