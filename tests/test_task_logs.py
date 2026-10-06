from jira_codex_agent.storage.database import Database


def test_log_cursor_isolates_tasks_and_survives_reopen(tmp_path):
    path = tmp_path / 'state.db'
    db = Database(path)
    db.initialize()
    db.append_event('TM-1', 'command', 'running tests')
    db.append_event('TM-2', 'command', 'other task')
    cursor = db.events('TM-1')[0]['id']
    db.append_event('TM-1', 'result', 'finished')
    reopened = Database(path)
    events = reopened.events('TM-1', cursor)
    assert len(events) == 1
    assert events[0]['message'] == 'finished'
    assert events[0]['timestamp']


def test_log_message_is_bounded(tmp_path):
    db = Database(tmp_path / 'state.db')
    db.initialize()
    db.append_event('TM-1', 'stdout', 'x' * 50000)
    assert len(db.events('TM-1')[0]['message']) == 32000
