import base64
from types import SimpleNamespace

from jira_codex_agent.config import Settings, DEFAULT_TASK_INSTRUCTIONS
from jira_codex_agent.scheduler.orchestrator import Orchestrator


def test_custom_prompt_preserves_multiline_unicode_and_issue_context():
    instructions = 'Chạy test trước khi hoàn tất.\nGiữ nguyên "API" và {schema}.\nKhông push.'
    settings = Settings.model_construct(codex_prompt_base64=base64.b64encode(instructions.encode()).decode())
    agent = Orchestrator(settings, None, None, None, None, None)
    issue = SimpleNamespace(key='TM-42', summary='Implement feature', fields=SimpleNamespace(description=None))
    prompt = agent._prompt(issue)
    assert instructions in prompt
    assert 'TM-42: Implement feature' in prompt
    assert 'No description supplied.' in prompt


def test_blank_prompt_uses_original_default():
    assert Settings.model_construct(codex_prompt_base64='').task_instructions == DEFAULT_TASK_INSTRUCTIONS
    encoded = base64.b64encode(b' \n ').decode()
    assert Settings.model_construct(codex_prompt_base64=encoded).task_instructions == DEFAULT_TASK_INSTRUCTIONS
