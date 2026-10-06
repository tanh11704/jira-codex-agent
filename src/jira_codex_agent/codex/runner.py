from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from .models import CodexRunResult, RunOutcome


class CodexRunner:
    def __init__(self, command: str = "codex", model: str | None = None, timeout: int = 7200) -> None:
        self.command = command
        self.model = model
        self.timeout = timeout

    async def run(self, prompt: str, cwd: Path, *, session_id: str | None = None,
                  on_event: Callable[[dict], None] | None = None) -> CodexRunResult:
        args = [self.command, "exec", "--json", "--sandbox", "workspace-write", "--cd", str(cwd)]
        if self.model:
            args.extend(["--model", self.model])
        if session_id:
            args.extend(["resume", session_id])
        args.append("-")
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        events = 0
        final_message = ""

        async def consume_stdout() -> None:
            nonlocal events, final_message, session_id
            async for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                events += 1
                session_id = session_id or event.get("thread_id")
                if on_event:
                    on_event(event)
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        final_message = item.get("text", final_message)

        stdout_task = asyncio.create_task(consume_stdout())
        async def consume_stderr() -> bytes:
            tail = bytearray()
            while chunk := await process.stderr.read(8192):
                tail.extend(chunk)
                del tail[:-65536]
            return bytes(tail)

        stderr_task = asyncio.create_task(consume_stderr())
        async def complete() -> bytes:
            await process.wait()
            await stdout_task
            return await stderr_task

        async def cleanup() -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        try:
            stderr = (await asyncio.wait_for(complete(), timeout=self.timeout)).decode(errors="replace").strip()
        except TimeoutError:
            await cleanup()
            return CodexRunResult(outcome=RunOutcome.TIMED_OUT, events=events, session_id=session_id, error='Task timed out; worktree preserved')
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            raise

        outcome = RunOutcome.SUCCEEDED if process.returncode == 0 else RunOutcome.FAILED
        return CodexRunResult(
            outcome=outcome,
            exit_code=process.returncode,
            final_message=final_message,
            session_id=session_id,
            events=events,
            error=(stderr or None) if outcome == RunOutcome.FAILED else None,
        )
