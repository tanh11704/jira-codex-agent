from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

# Claude only reads: it may inspect files and git state but never edits the worktree Codex owns.
TOOLS = ['Read', 'Grep', 'Glob', 'Bash']
ALLOWED_TOOLS = ['Read', 'Grep', 'Glob', 'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)',
                 'Bash(git show:*)', 'Bash(git branch:*)', 'Bash(git rev-parse:*)', 'Bash(git ls-files:*)',
                 'Bash(ls:*)', 'Bash(pwd)']


class ClaudeResult(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None
    session_id: str | None = None
    input_tokens: int = 0  # including cache reads and writes
    output_tokens: int = 0


class ClaudeRunner:
    """Runs one headless `claude -p` turn whose final answer must match a JSON schema."""

    def __init__(self, command: str = 'claude', model: str | None = None, effort: str | None = None,
                 timeout: int = 1800, max_turns: int = 0) -> None:
        self.command, self.model, self.effort, self.timeout = command, model, effort, timeout
        self.max_turns = max_turns

    def arguments(self, schema: dict) -> list[str]:
        args = [self.command, '-p', '--output-format', 'stream-json', '--verbose',
                '--permission-mode', 'dontAsk', '--json-schema', json.dumps(schema),
                '--tools', *TOOLS, '--allowedTools', *ALLOWED_TOOLS]
        if self.model:
            args.extend(['--model', self.model])
        if self.effort:
            args.extend(['--effort', self.effort])
        if self.max_turns:
            args.extend(['--max-turns', str(self.max_turns)])
        return args

    async def run(self, prompt: str, cwd: Path, *, schema: dict,
                  on_event: Callable[[dict], None] | None = None) -> ClaudeResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.arguments(schema), cwd=cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=4 * 1024 * 1024)
        except OSError as exc:
            return ClaudeResult(ok=False, error=f'Could not start {self.command}: {exc}')
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        final: dict = {}
        stderr_tail = bytearray()

        async def read_stdout() -> None:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'result':
                    final.update(event)
                elif on_event:
                    for item in describe(event):
                        on_event(item)

        async def read_stderr() -> None:
            while chunk := await process.stderr.read(8192):
                stderr_tail.extend(chunk)
                del stderr_tail[:-16384]

        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
        try:
            async with asyncio.timeout(self.timeout):
                await process.wait()
                await asyncio.gather(*readers)
        except TimeoutError:
            return ClaudeResult(ok=False, error=f'Claude timed out after {self.timeout}s')
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        usage = final.get('usage') or {}
        meta = dict(session_id=final.get('session_id'), output_tokens=usage.get('output_tokens') or 0,
                    input_tokens=sum(usage.get(key) or 0 for key in (
                        'input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')))
        if not final or final.get('is_error') or final.get('subtype') != 'success':
            # Limits end the run with e.g. subtype error_max_turns and a readable "errors" list.
            error = ('; '.join(map(str, final.get('errors') or [])) or final.get('result') or final.get('subtype')
                     or stderr_tail.decode(errors='replace').strip())
            return ClaudeResult(ok=False, error=str(error or f'claude exited with {process.returncode}'), **meta)
        data = final.get('structured_output')
        if not isinstance(data, dict):
            try:
                data = json.loads(final.get('result') or '')
            except ValueError:
                data = None
        if not isinstance(data, dict):
            return ClaudeResult(ok=False, error='Claude did not return the structured answer', **meta)
        return ClaudeResult(ok=True, data=data, **meta)


def describe(event: dict) -> list[dict]:
    """Turn stream-json assistant messages into task log entries; thinking and system noise are dropped."""
    if event.get('type') != 'assistant':
        return []
    items = []
    for block in (event.get('message') or {}).get('content') or []:
        if block.get('type') == 'text' and block.get('text', '').strip():
            items.append({'type': 'claude.message', 'message': block['text']})
        elif block.get('type') == 'tool_use' and block.get('name') != 'StructuredOutput':
            arguments = block.get('input') or {}
            detail = arguments.get('command') or arguments.get('file_path') or arguments.get('pattern') \
                or json.dumps(arguments, ensure_ascii=False)
            items.append({'type': 'claude.tool', 'message': f"{block.get('name')}: {detail}"})
    return items
