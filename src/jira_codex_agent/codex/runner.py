from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .models import CodexRunResult, RunOutcome


class CodexRunner:
    def __init__(self, command: str = "codex", model: str | None = None, timeout: int = 7200) -> None:
        self.command = command
        self.model = model
        self.timeout = timeout

    async def run(self, prompt: str, cwd: Path) -> CodexRunResult:
        args = [self.command, "exec", "--json", "--sandbox", "workspace-write", "--cd", str(cwd)]
        if self.model:
            args.extend(["--model", self.model])
        args.append("-")
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        events = 0
        final_message = ""
        session_id = None

        async def consume_stdout() -> None:
            nonlocal events, final_message, session_id
            async for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                events += 1
                session_id = session_id or event.get("thread_id")
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        final_message = item.get("text", final_message)

        stdout_task = asyncio.create_task(consume_stdout())
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout)
            await stdout_task
            stderr = (await stderr_task).decode(errors="replace").strip()
        except TimeoutError:
            process.terminate()
            await process.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            return CodexRunResult(outcome=RunOutcome.TIMED_OUT, events=events, session_id=session_id)

        outcome = RunOutcome.SUCCEEDED if process.returncode == 0 else RunOutcome.FAILED
        return CodexRunResult(
            outcome=outcome,
            exit_code=process.returncode,
            final_message=final_message,
            session_id=session_id,
            events=events,
            error=(stderr or None) if outcome == RunOutcome.FAILED else None,
        )
