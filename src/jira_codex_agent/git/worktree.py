from __future__ import annotations

import asyncio
import re
from pathlib import Path


class GitError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, repository: Path, root: Path, base_branch: str = "main") -> None:
        self.repository = repository
        self.root = root
        self.base_branch = base_branch

    @staticmethod
    def branch_for(issue_key: str) -> str:
        safe = re.sub(r"[^a-z0-9-]+", "-", issue_key.lower()).strip("-")
        return f"codex/{safe}"

    async def create(self, issue_key: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / issue_key.lower()
        if path.exists():
            return path
        branch = self.branch_for(issue_key)
        branches = await self._git("branch", "--list", branch)
        if branches.strip():
            await self._git("worktree", "add", str(path), branch)
        else:
            await self._git("worktree", "add", "-b", branch, str(path), self.base_branch)
        return path

    async def _git(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(self.repository), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise GitError(stderr.decode(errors="replace").strip())
        return stdout.decode(errors="replace")
