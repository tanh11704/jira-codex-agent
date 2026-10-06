import pytest
from jira_codex_agent.storage.database import Database, TaskState


def test_restart_preserves_session_worktree_and_progress(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('TM-141', 'Recover')
    db.update_task('TM-141', TaskState.CODING, session_id='thread-123', worktree=str(tmp_path), progress='last event')
    db.recover_interrupted()
    row = db.list_tasks()[0]
    assert row['state'] == 'interrupted'
    assert row['session_id'] == 'thread-123'
    assert row['progress'] == 'last event'
    db.request_resume('TM-141')
    assert db.list_tasks()[0]['state'] == 'resume_pending'
    with pytest.raises(ValueError):
        db.request_resume('TM-141')
