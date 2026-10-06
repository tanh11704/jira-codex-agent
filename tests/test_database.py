from jira_codex_agent.storage.database import Database, TaskState


def test_task_lifecycle(tmp_path) -> None:
    database = Database(tmp_path / "state.db")
    database.initialize()
    database.upsert_task("DATN-1", "Build it")
    database.update_task("DATN-1", TaskState.CODING, worktree="/tmp/work")
    task = database.list_tasks()[0]
    assert task["state"] == "coding"
    assert task["worktree"] == "/tmp/work"
    assert database.is_paused() is False
    database.set_paused(True)
    assert database.is_paused() is True
