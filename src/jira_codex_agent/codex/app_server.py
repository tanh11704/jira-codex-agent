import asyncio
import json
import os
import signal
import uuid

from .models import CodexRunResult, RunOutcome


def approval_matches(details, allowed):
    """A request a human allowed after it was declined: same kind and, for commands, the same command."""
    return details.get('kind') == allowed.get('kind') and (details.get('command') or None) == (allowed.get('command') or None)


class CodexRunner:
    def __init__(self, command='codex', model=None, timeout=7200, approvals_reviewer='user', approval_timeout=0,
                 reasoning_effort=None, service_tier=None):
        if approvals_reviewer not in ('user', 'auto_review'):
            raise ValueError('Unsupported approval reviewer')
        self.approvals_reviewer = approvals_reviewer
        # Seconds to wait for a human before declining; 0 waits forever.
        self.approval_timeout = approval_timeout
        self.command, self.model, self.timeout = command, model, timeout
        self.reasoning_effort, self.service_tier = reasoning_effort, service_tier
        self.approvals, self._answers = {}, {}

    def answer_approval(self, token, decision):
        if decision not in ('accept', 'decline'):
            raise ValueError('Only accept or decline is supported')
        future = self._answers.get(token)
        if future is None or future.done():
            raise ValueError('Approval expired or already answered; refresh dashboard')
        future.set_result(decision)

    async def archive_thread(self, thread_id):
        """Archive a Codex thread so discarded work disappears from Codex history."""
        process = await asyncio.create_subprocess_exec(
            self.command, 'app-server', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

        async def call(key, method, params):
            process.stdin.write((json.dumps({'id': key, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get('id') == key and 'method' not in message:
                    if 'error' in message:
                        raise RuntimeError(str(message['error']))
                    return message.get('result', {})
            raise RuntimeError('Codex app-server closed')

        try:
            async with asyncio.timeout(20):
                await call(1, 'initialize', {'clientInfo': {'name': 'jira_codex_agent', 'version': '0.1.0'}})
                process.stdin.write(b'{"method": "initialized", "params": {}}\n')
                await call(2, 'thread/archive', {'threadId': thread_id})
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def run(self, prompt, cwd, *, session_id=None, on_event=None, output_schema=None, preapproved=None):
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

            def answer(decision):
                if method == 'item/permissions/requestApproval':
                    return {'permissions': params.get('permissions', {}) if decision == 'accept' else {}, 'scope': 'turn'}
                return {'decision': decision}

            if any(approval_matches(details, allowed) for allowed in preapproved or []):
                await send({'id': message['id'], 'result': answer('accept')})
                emit({'type': 'approval.preapproved', 'message': 'Allowed earlier by a human: ' + (details['command'] or method)})
                return
            future = asyncio.get_running_loop().create_future()
            self.approvals[token], self._answers[token] = details, future
            emit({'type': 'approval.pending', 'message': 'Waiting for approval', 'approval': details})
            try:
                try:
                    decision = await (asyncio.wait_for(future, self.approval_timeout) if self.approval_timeout else future)
                except TimeoutError:
                    # Nobody is watching: decline so Codex continues (or reports) instead of hanging.
                    decision = 'decline'
                    emit({'type': 'approval.timeout', 'approval_id': token, 'approval': details,
                          'message': f'No answer within {self.approval_timeout}s; declined automatically'})
                await send({'id': message['id'], 'result': answer(decision)})
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
            config = {'cwd': str(cwd), 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write',
                      'approvalsReviewer': self.approvals_reviewer}
            if self.model:
                config['model'] = self.model
            if self.service_tier:
                config['serviceTier'] = self.service_tier
            if session_id:
                config['threadId'] = session_id
            thread = await request('thread/resume' if session_id else 'thread/start', config)
            session_id = thread['thread']['id']
            emit({'type': 'thread.started', 'thread_id': session_id})
            turn = {'threadId': session_id, 'input': [{'type': 'text', 'text': prompt}]}
            if self.reasoning_effort:
                turn['effort'] = self.reasoning_effort
            if output_schema:
                turn['outputSchema'] = output_schema
            await request('turn/start', turn)
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
