from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections.abc import Callable
from pathlib import Path

from jira_codex_agent.agents import DEFAULT_ANTIGRAVITY_MODEL
from jira_codex_agent.claude.runner import AgentResult

# Headless agy auto-denies anything not allowed, except writes inside the workspace, so read-only passes deny those.
READ_COMMANDS = ['git status', 'git diff', 'git log', 'git show', 'git branch', 'git rev-parse', 'git ls-files', 'ls', 'pwd']
# Coding passes may run any command (builds, tests, dev server, screenshots) but never rewrite git history:
# the task rules require uncommitted changes for the human.
DENIED_GIT = ['git commit', 'git push', 'git reset', 'git rebase', 'git merge', 'git checkout', 'git switch',
              'git stash', 'git clean']
# Failures that mean "cannot run now" rather than "the work failed"; the caller may hand the work to another agent.
UNAVAILABLE = re.compile(r'quota|rate.?limit|resource.?exhausted|too many requests|\b429\b|credits?|'
                         r'not (signed|logged) in|sign.?in|log.?in required|unauthenticated|auth_required|could not start', re.I)


def permissions(cwd: Path, write: bool) -> dict:
    root = str(cwd.resolve())
    if write:
        return {'allow': [f'read_file({root})', f'write_file({root})', 'command(*)'],
                'deny': [f'command({command})' for command in DENIED_GIT]}
    return {'allow': [f'read_file({root})'] + [f'command({command})' for command in READ_COMMANDS],
            'deny': [f'write_file({root})']}


def unavailable(error: str | None) -> bool:
    return bool(error and UNAVAILABLE.search(error))


class AntigravityRunner:
    """Runs one headless `agy --print` turn whose final answer must match a JSON schema.

    Same contract as ClaudeRunner: read-only by default, write=True for coding, resume=<conversation id>."""

    def __init__(self, command: str = 'agy', model: str | None = DEFAULT_ANTIGRAVITY_MODEL, timeout: int = 1800) -> None:
        self.command, self.model, self.timeout = command, model, timeout

    def arguments(self, prompt: str, cwd: Path, schema: dict, *, write: bool = False, resume: str | None = None,
                  model: str | None = None, effort: str | None = None) -> list[str]:
        args = [self.command, '--output-format', 'stream-json', '--disable-slash-commands',
                '--json-schema', json.dumps(schema), '--settings', json.dumps({'permissions': permissions(cwd, write)})]
        if model := model or self.model:
            args.extend(['--model', model])
        if effort:
            args.extend(['--effort', effort])
        if resume:
            args.extend(['--conversation', resume])
        # Attached to the flag so a prompt can never be read as an option.
        args.append(f'--print={prompt}')
        return args

    async def run(self, prompt: str, cwd: Path, *, schema: dict,
                  on_event: Callable[[dict], None] | None = None, write: bool = False,
                  session_id: str | None = None, resume: str | None = None,
                  timeout: int | None = None, model: str | None = None, effort: str | None = None,
                  on_session: Callable[[str], None] | None = None) -> AgentResult:
        # session_id is ignored: agy names the conversation itself and reports it in the init event (on_session).
        timeout = timeout or self.timeout
        try:
            process = await asyncio.create_subprocess_exec(
                *self.arguments(prompt, cwd, schema, write=write, resume=resume, model=model, effort=effort), cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=4 * 1024 * 1024)
        except OSError as exc:
            return AgentResult(ok=False, error=f'Could not start {self.command}: {exc}')
        assert process.stdout and process.stderr

        final: dict = {}
        started: dict = {}
        texts: dict[int, str] = {}
        stderr_tail = bytearray()

        async def read_stdout() -> None:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get('event')
                if kind == 'result':
                    final.update(event.get('result') or {})
                    return  # agy may linger after its answer; nothing after it matters
                if kind == 'init':
                    started.update(event.get('init') or {}, conversation_id=event.get('conversation_id'))
                    if on_session and event.get('conversation_id'):
                        on_session(event['conversation_id'])
                elif kind == 'step_update' and on_event:
                    for item in describe(event.get('step_update') or {}, texts):
                        on_event(item)

        async def read_stderr() -> None:
            while chunk := await process.stderr.read(8192):
                stderr_tail.extend(chunk)
                del stderr_tail[:-16384]

        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
        try:
            async with asyncio.timeout(timeout):
                await readers[0]
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                pass
        except TimeoutError:
            return AgentResult(ok=False, session_id=started.get('conversation_id'),
                               error=f'Antigravity timed out after {timeout}s')
        finally:
            if process.returncode is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                        break
                    except TimeoutError:
                        continue
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        usage = final.get('usage') or {}
        meta = dict(session_id=final.get('conversation_id') or started.get('conversation_id'),
                    model=started.get('model') or model or self.model,
                    input_tokens=(usage.get('input_tokens') or 0) + (usage.get('cache_read_tokens') or 0),
                    output_tokens=(usage.get('output_tokens') or 0) + (usage.get('thinking_tokens') or 0))
        if not final or final.get('status') != 'SUCCESS':
            error = (final.get('error') or final.get('error_message') or final.get('status')
                     or stderr_tail.decode(errors='replace').strip())
            if isinstance(error, dict):
                error = error.get('message') or json.dumps(error)
            return AgentResult(ok=False, error=str(error or f'agy exited with {process.returncode}'), **meta)
        data = final.get('structured_output')
        if not isinstance(data, dict):
            try:
                data = json.loads(final.get('response') or '')
            except ValueError:
                data = None
        if not isinstance(data, dict):
            denied = ', '.join(item.get('display_name') or item.get('action') or '?' for item in final.get('denied_actions') or [])
            reason = stderr_tail.decode(errors='replace').strip().splitlines()
            return AgentResult(ok=False, error='Antigravity did not return the structured answer'
                               + (f' (denied: {denied})' if denied else '') + (f': {reason[-1]}' if reason else ''), **meta)
        return AgentResult(ok=True, data=data, **meta)


def describe(step: dict, texts: dict[int, str]) -> list[dict]:
    """Turn stream-json steps into task log entries: tool calls, failures and the agent's own messages."""
    kind, state = step.get('step_type'), step.get('state')
    if kind == 'agent_response':
        index = step.get('step_index', -1)
        texts[index] = texts.get(index, '') + (step.get('text_delta') or '')
        if state != 'DONE':
            return []
        text = texts.pop(index, '').strip()
        try:
            if isinstance(json.loads(text), dict):
                return []  # the final structured answer is stored as the result, not logged twice
        except ValueError:
            pass
        return [{'type': 'antigravity.message', 'message': text}] if text else []
    if kind != 'tool' or step.get('tool_name') in (None, 'update_steps', 'finish'):
        return []
    info = step.get('tool_info') or {}
    parameters = info.get('parameters') or {}
    detail = (parameters.get('CommandLine') or parameters.get('Path') or parameters.get('AbsolutePath')
              or parameters.get('Query') or parameters.get('Url') or json.dumps(parameters, ensure_ascii=False))
    if state == 'ACTIVE':
        return [{'type': 'antigravity.tool', 'message': f"{step['tool_name']}: {detail}"}]
    if state == 'ERROR':
        error = (info.get('error') or {}).get('message') or 'failed'
        return [{'type': 'antigravity.tool', 'message': f"{step['tool_name']} failed: {detail}\n{error}"}]
    return []
