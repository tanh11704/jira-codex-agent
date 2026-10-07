from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from .models import QuotaSnapshot, QuotaWindow


class QuotaReader:
    """Read ChatGPT-plan Codex quota from a short-lived app-server process."""

    def __init__(self, command: str = "codex", timeout: float = 15) -> None:
        self.command = command
        self.timeout = timeout
        self.cached: QuotaSnapshot | None = None
        self.checked_at: datetime | None = None
        self._checked_monotonic = 0.0
        self._lock = asyncio.Lock()

    async def read(self, *, force: bool = False) -> QuotaSnapshot:
        async with self._lock:
            if not force and self.cached is not None and time.monotonic() - self._checked_monotonic < 30:
                return self.cached
            self.cached = await self._read_live()
            self.checked_at = datetime.now(timezone.utc)
            self._checked_monotonic = time.monotonic()
            return self.cached

    async def _read_live(self) -> QuotaSnapshot:
        process = await asyncio.create_subprocess_exec(
            self.command,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin and process.stdout
        try:
            result = await asyncio.wait_for(self._request(process), timeout=self.timeout)
            return self._parse(result)
        except Exception as exc:
            return QuotaSnapshot(error=str(exc))
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def _request(self, process: asyncio.subprocess.Process) -> dict:
        assert process.stdin and process.stdout
        initialize = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "jira-codex-agent", "version": "0.1.0"}}}
        process.stdin.write((json.dumps(initialize) + "\n").encode())
        await process.stdin.drain()

        await self._response(process, 1)
        messages = [
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}},
        ]
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        return await self._response(process, 2)

    @staticmethod
    async def _response(process: asyncio.subprocess.Process, request_id: int) -> dict:
        assert process.stdout
        while line := await process.stdout.readline():
            payload = json.loads(line)
            if payload.get("id") == request_id:
                if "error" in payload:
                    raise RuntimeError(payload["error"].get("message", "quota request failed"))
                return payload.get("result", {})
        raise RuntimeError("app-server closed before returning quota")

    @staticmethod
    def _parse(result: dict) -> QuotaSnapshot:
        limits = result.get("rateLimits") or {}

        def window(value: dict | None) -> QuotaWindow | None:
            if not value:
                return None
            reset = value.get("resetsAt")
            return QuotaWindow(
                used_percent=value.get("usedPercent", 0),
                window_minutes=value.get("windowDurationMins"),
                resets_at=datetime.fromtimestamp(reset, tz=timezone.utc) if reset else None,
            )

        return QuotaSnapshot(
            primary=window(limits.get("primary")),
            secondary=window(limits.get("secondary")),
            ordinary_usage_allowed=result.get("ordinaryUsageAllowed"),
        )
