from jira_codex_agent.storage.database import Database, TaskState


def test_scope_change_archives_stale_queue_but_preserves_review(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.upsert_task('HAN-1', 'Old project')
    db.upsert_task('TM-1', 'Old sprint')
    db.upsert_task('TM-2', 'Review history')
    db.update_task('TM-2', TaskState.REVIEW)
    db.reconcile_queue([('TM-3', 'Current sprint')])
    assert {row['issue_key'] for row in db.list_tasks()} == {'TM-2', 'TM-3'}
    assert {row['issue_key'] for row in db.list_tasks(TaskState.QUEUED)} == {'TM-3'}
    db.reconcile_queue([('HAN-1', 'Back in scope')])
    assert {row['issue_key'] for row in db.list_tasks(TaskState.QUEUED)} == {'HAN-1'}
