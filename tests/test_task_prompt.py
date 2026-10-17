import base64
from pathlib import Path
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


def test_swift_and_daemon_share_same_default_text():
    source = Path('macos/JiraCodexAgentUI/Sources/JiraCodexAgentUI/AgentStore.swift').read_text()
    swift_prompt = source.split('static let defaultTaskPrompt = """\n', 1)[1].split('\n    """', 1)[0]
    assert '\n'.join(line[4:] for line in swift_prompt.splitlines()) == DEFAULT_TASK_INSTRUCTIONS


def test_default_requires_repository_rules_and_safe_handoff():
    for requirement in ('RULES.md', 'CONTRIBUTING.md', 'feature/TM-142', 'codex/tm-142',
                        'BLOCKED_BRANCH', 'make check', 'UNCOMMITTED', 'Conventional Commit'):
        assert requirement in DEFAULT_TASK_INSTRUCTIONS
