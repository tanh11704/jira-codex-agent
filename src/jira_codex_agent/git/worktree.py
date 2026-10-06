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

    async def remove(self, issue_key: str, path: Path) -> None:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+', issue_key):
            raise GitError('Invalid issue key')
        expected = self.root.resolve() / issue_key.lower()
        if path.is_symlink() or path.resolve() != expected or expected == self.repository.resolve():
            raise GitError('Refusing to remove a path outside the task worktree')
        registered = await self._git('worktree', 'list', '--porcelain')
        if f'worktree {expected}' not in registered.splitlines():
            raise GitError('Path is not a registered worktree of this repository')
        await self._git('worktree', 'remove', '--force', str(expected))
