from unittest.mock import AsyncMock

import pytest

from jira_codex_agent.git.worktree import GitError, WorktreeManager


@pytest.mark.asyncio
async def test_cleanup_rejects_repository_and_unregistered_paths(tmp_path):
    repo = tmp_path / 'repo'
    root = tmp_path / 'worktrees'
    manager = WorktreeManager(repo, root)
    manager._git = AsyncMock(return_value='')
    with pytest.raises(GitError):
        await manager.remove('TM-1', repo)
    manager._git.assert_not_awaited()
    with pytest.raises(GitError):
        await manager.remove('TM-1', root / 'tm-1')
    assert manager._git.await_count == 1


@pytest.mark.asyncio
async def test_cleanup_removes_only_exact_registered_worktree(tmp_path):
    root = tmp_path / 'worktrees'
    path = root / 'tm-1'
    manager = WorktreeManager(tmp_path / 'repo', root)
    manager._git = AsyncMock(side_effect=[f'worktree {path}\nbranch refs/heads/codex/tm-1\n', ''])
    await manager.remove('TM-1', path)
    manager._git.assert_awaited_with('worktree', 'remove', '--force', str(path))
