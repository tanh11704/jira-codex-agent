import asyncio
import json
import os
import signal
import uuid

from .models import CodexRunResult, RunOutcome


class CodexRunner:
    def __init__(self, command='codex', model=None, timeout=7200):
        self.command, self.model, self.timeout = command, model, timeout
        self.approvals, self._answers = {}, {}

    def answer_approval(self, token, decision):
        if decision not in ('accept', 'decline'):
            raise ValueError('Only accept or decline is supported')
        future = self._answers.get(token)
        if future is None or future.done():
            raise ValueError('Approval expired or already answered; refresh dashboard')
        future.set_result(decision)

    async def run(self, prompt, cwd, *, session_id=None, on_event=None):
        process = await asyncio.create_subprocess_exec(
            self.command, 'app-server', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=4 * 1024 * 1024)
        responses, items, approval_tasks = {}, {}, {}
        finished = asyncio.get_running_loop().create_future()
        events, final_message, next_id = 0, '', 0
        stderr_tail = bytearray()

        def emit(event):
            nonlocal events
            events += 1
            if on_event:
                on_event(event)

        async def send(message):
            process.stdin.write((json.dumps(message) + '\n').encode())
            await process.stdin.drain()

        async def request(method, params):
            nonlocal next_id
            next_id += 1
            key = next_id
            responses[key] = asyncio.get_running_loop().create_future()
            try:
                await send({'id': key, 'method': method, 'params': params})
                return await asyncio.wait_for(responses[key], 30)
            finally:
                responses.pop(key, None)

        async def approval(message):
            params, method = message.get('params', {}), message['method']
            if method not in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                              'item/permissions/requestApproval'):
                await send({'id': message['id'], 'error': {'code': -32601,
                            'message': 'Interactive request not supported by Jira dashboard'}})
                emit({'type': 'approval.unsupported', 'message': method})
                if not finished.done():
                    finished.set_exception(RuntimeError('Unsupported interactive request: ' + method))
                return
            token = uuid.uuid4().hex
            item = items.get(params.get('itemId'), {})
            details = {'id': token, 'kind': method, 'threadId': params.get('threadId'),
                       'command': params.get('command') or item.get('command'),
                       'cwd': params.get('cwd') or item.get('cwd') or str(cwd),
                       'reason': params.get('reason'), 'details': json.dumps(params, ensure_ascii=False),
                       'changes': json.dumps(item.get('changes', []), ensure_ascii=False),
                       'scope': 'turn' if method == 'item/permissions/requestApproval' else 'request'}
            if params.get('networkApprovalContext'):
                network = params['networkApprovalContext']
                details['command'] = 'Network access: ' + network.get('protocol', '') + '://' + network.get('host', '')
            future = asyncio.get_running_loop().create_future()
            self.approvals[token], self._answers[token] = details, future
            emit({'type': 'approval.pending', 'message': 'Waiting for approval', 'approval': details})
            try:
                decision = await future
                if method == 'item/permissions/requestApproval':
                    result = {'permissions': params.get('permissions', {}) if decision == 'accept' else {}, 'scope': 'turn'}
                else:
                    result = {'decision': decision}
                await send({'id': message['id'], 'result': result})
                emit({'type': 'approval.answered', 'message': decision, 'approval_id': token})
            finally:
                self.approvals.pop(token, None)
                self._answers.pop(token, None)
                emit({'type': 'approval.cleared', 'message': 'Approval closed', 'approval_id': token})

        async def read_stdout():
            nonlocal final_message
            try:
                async for line in process.stdout:
                    message = json.loads(line)
                    if 'method' not in message:
                        future = responses.get(message.get('id'))
                        if future and not future.done():
                            if 'error' in message:
                                future.set_exception(RuntimeError(str(message['error'])))
                            else:
                                future.set_result(message.get('result', {}))
                        continue
                    if 'id' in message:
                        task = asyncio.create_task(approval(message))
                        approval_tasks[message['id']] = task
                        def check_approval(done):
                            if not done.cancelled():
                                error = done.exception()
                                if error and not finished.done():
                                    finished.set_exception(error)
                        task.add_done_callback(check_approval)
                        continue
                    method, params = message['method'], message.get('params', {})
                    item = params.get('item', {})
                    if item.get('id'):
                        items[item['id']] = item
                    emit({'type': method, **params, 'message': params.get('delta', '')})
                    if method == 'serverRequest/resolved':
                        task = approval_tasks.get(params.get('requestId'))
                        if task:
                            task.cancel()
                    if method == 'item/completed' and item.get('type') == 'agentMessage':
                        final_message = item.get('text', final_message)
                    if method == 'turn/completed' and not finished.done():
                        finished.set_result(params['turn'])
            except Exception as exc:
                if not finished.done():
                    finished.set_exception(exc)
            finally:
                error = RuntimeError('Codex app-server disconnected: ' + stderr_tail.decode(errors='replace'))
                for future in responses.values():
                    if not future.done():
                        future.set_exception(error)
                if not finished.done():
                    finished.set_exception(error)

        async def read_stderr():
            while chunk := await process.stderr.read(8192):
                stderr_tail.extend(chunk)
                del stderr_tail[:-65536]
                emit({'type': 'stderr', 'message': chunk.decode(errors='replace')})

        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
        try:
            await request('initialize', {'clientInfo': {'name': 'jira_codex_agent', 'version': '0.1.0'}})
            await send({'method': 'initialized', 'params': {}})
            config = {'cwd': str(cwd), 'approvalPolicy': 'on-request', 'sandbox': 'workspaceWrite',
                      'approvalsReviewer': 'user'}
            if self.model:
                config['model'] = self.model
            if session_id:
                config['threadId'] = session_id
            thread = await request('thread/resume' if session_id else 'thread/start', config)
            session_id = thread['thread']['id']
            emit({'type': 'thread.started', 'thread_id': session_id})
            await request('turn/start', {'threadId': session_id, 'input': [{'type': 'text', 'text': prompt}]})
            remaining = float(self.timeout)
            while not finished.done():
                started = asyncio.get_running_loop().time()
                waiting = bool(self.approvals)
                await asyncio.wait([finished], timeout=min(0.25, remaining))
                if not waiting and not self.approvals:
                    remaining -= asyncio.get_running_loop().time() - started
                if remaining <= 0 and not finished.done():
                    return CodexRunResult(outcome=RunOutcome.TIMED_OUT, session_id=session_id, events=events,
                                          error='Task timed out; worktree preserved')
            turn = finished.result()
            succeeded = turn.get('status') == 'completed'
            return CodexRunResult(outcome=RunOutcome.SUCCEEDED if succeeded else RunOutcome.FAILED,
                                  session_id=session_id, events=events, final_message=final_message,
                                  error=None if succeeded else str(turn.get('error') or turn.get('status')))
        finally:
            for task in approval_tasks.values():
                task.cancel()
            await asyncio.gather(*approval_tasks.values(), return_exceptions=True)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if finished.done() and not finished.cancelled():
                finished.exception()
