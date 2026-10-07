import asyncio
import json

import pytest

from jira_codex_agent.codex.runner import CodexRunner
from jira_codex_agent.codex.models import RunOutcome


SERVER = '''#!/usr/bin/env python3
import json, sys
def send(m):
 print(json.dumps(m), flush=True)
for raw in sys.stdin:
 m = json.loads(raw)
 method = m.get('method')
 if method == 'initialize':
  send({'id':m['id'],'result':{}})
 elif method in ('thread/start','thread/resume'):
  assert m['params']['approvalPolicy'] == 'on-request'
  assert m['params']['sandbox'] == 'workspace-write'
  assert m['params']['approvalsReviewer'] == 'user'
  send({'id':m['id'],'result':{'thread':{'id':m['params'].get('threadId','thread-1')}}})
 elif method == 'turn/start':
  send({'id':m['id'],'result':{'turn':{'id':'turn-1'}}})
  send({'method':'item/started','params':{'item':{'id':'item-1','type':'commandExecution','command':'echo demo','cwd':'/test'}}})
  send({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'threadId':m['params']['threadId'],'turnId':'turn-1','itemId':'item-1','reason':'Need permission'}})
 elif m.get('id') == 'approval-1':
  send({'method':'item/completed','params':{'item':{'type':'agentMessage','text':json.dumps(m['result'])}}})
  send({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
'''


@pytest.fixture
def server(tmp_path):
    path = tmp_path / 'fake-codex'
    path.write_text(SERVER)
    path.chmod(0o700)
    return str(path)


async def wait_approval(runner):
    async with asyncio.timeout(3):
        while not runner.approvals:
            await asyncio.sleep(0.01)
    return next(iter(runner.approvals))


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['accept', 'decline'])
async def test_approval_roundtrip(server, tmp_path, decision):
    runner = CodexRunner(server, timeout=1)
    events = []
    task = asyncio.create_task(runner.run('Do work', tmp_path, session_id='existing-thread', on_event=events.append))
    token = await wait_approval(runner)
    assert runner.approvals[token]['command'] == 'echo demo'
    await asyncio.sleep(1.2)  # Human wait must not consume the 1-second execution timeout.
    assert not task.done()
    runner.answer_approval(token, decision)
    with pytest.raises(ValueError):
        runner.answer_approval(token, decision)
    result = await asyncio.wait_for(task, 3)
    assert result.outcome == RunOutcome.SUCCEEDED
    assert json.loads(result.final_message) == {'decision': decision}
    assert result.session_id == 'existing-thread'
    assert not runner.approvals
    assert any(e.get('thread_id') == 'existing-thread' for e in events)


@pytest.mark.asyncio
async def test_stop_clears_pending_approval(server, tmp_path):
    runner = CodexRunner(server)
    task = asyncio.create_task(runner.run('Do work', tmp_path))
    token = await wait_approval(runner)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner.approvals
    with pytest.raises(ValueError):
        runner.answer_approval(token, 'accept')


def test_no_automatic_or_session_wide_approval():
    runner = CodexRunner()
    with pytest.raises(ValueError):
        runner.answer_approval('unknown', 'acceptForSession')


@pytest.mark.asyncio
async def test_auto_review_config_keeps_sandbox_and_manual_fallback(tmp_path):
    path = tmp_path / 'fake-codex'
    path.write_text(SERVER.replace("== 'user'", "== 'auto_review'"))
    path.chmod(0o700)
    runner = CodexRunner(str(path), approvals_reviewer='auto_review')
    task = asyncio.create_task(runner.run('Do work', tmp_path, session_id='existing-thread'))
    token = await wait_approval(runner)
    assert not task.done()
    runner.answer_approval(token, 'decline')
    assert (await asyncio.wait_for(task, 3)).outcome == RunOutcome.SUCCEEDED


def test_invalid_reviewer_rejected():
    with pytest.raises(ValueError):
        CodexRunner(approvals_reviewer='never')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['fileChange', 'permissions'])
@pytest.mark.parametrize('decision', ['accept', 'decline'])
async def test_file_and_permission_approvals(tmp_path, kind, decision):
    path = tmp_path / 'fake-codex'
    script = SERVER.replace('item/commandExecution/requestApproval', f'item/{kind}/requestApproval')
    script = script.replace("'reason':'Need permission'", "'reason':'Need permission','permissions':{'network':{'enabled':True}}")
    path.write_text(script)
    path.chmod(0o700)
    runner = CodexRunner(str(path))
    task = asyncio.create_task(runner.run('Do work', tmp_path))
    token = await wait_approval(runner)
    runner.answer_approval(token, decision)
    result = await asyncio.wait_for(task, 3)
    response = json.loads(result.final_message)
    if kind == 'permissions':
        assert response == {'scope': 'turn', 'permissions': {'network': {'enabled': True}} if decision == 'accept' else {}}
    else:
        assert response == {'decision': decision}
