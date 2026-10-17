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

    def _owns_branch(self, issue_key: str, branch: str) -> bool:
        # Exact key match: TM-1 must not match feature/TM-14-x.
        pattern = rf'(?<![a-z0-9]){re.escape(issue_key.lower())}(?![0-9])'
        return bool(branch) and branch != self.base_branch and re.search(pattern, branch.lower()) is not None

    async def discard(self, issue_key: str, path: Path | None) -> list[str]:
        """Remove the task worktree and the issue's own local branches. Returns deleted branches.

        Codex may have renamed codex/<key> to e.g. feature/<KEY>-slug, so the branch
        actually checked out in the worktree is deleted too, but only if it names this issue.
        """
        branches = {self.branch_for(issue_key)}
        if path is not None and path.is_dir():
            process = await asyncio.create_subprocess_exec(
                'git', '-C', str(path), 'branch', '--show-current',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await process.communicate()
            branches.add(stdout.decode(errors='replace').strip())
            await self.remove(issue_key, path)
        await self._git('worktree', 'prune')
        deleted = []
        for branch in sorted(b for b in branches if self._owns_branch(issue_key, b)):
            if (await self._git('branch', '--list', branch)).strip():
                await self._git('branch', '-D', branch)
                deleted.append(branch)
        return deleted
