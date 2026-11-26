"""Which agent does which job: planning, coding the backend and the frontend, reviewing and deciding."""
from __future__ import annotations

AGENTS = ('codex', 'claude', 'antigravity')
# Short names for passes and log lines; coder names for work handed between coding agents.
AGENT_NAMES = {'codex': 'Codex', 'claude': 'Claude', 'antigravity': 'Antigravity'}
CODER_NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 'antigravity': 'Antigravity'}

ROLES = ('plan', 'backend', 'frontend', 'review', 'decide')
# Agents each role may use. Read-only passes need a structured, read-only CLI run, which Codex does not offer here.
ROLE_AGENTS = {
    'plan': ('claude', 'antigravity'),
    'backend': ('codex', 'claude', 'antigravity'),
    'frontend': ('antigravity', 'claude', 'codex'),
    'review': ('claude', 'antigravity'),
    'decide': ('claude', 'antigravity'),
}
DEFAULT_AGENTS = {'plan': 'claude', 'backend': 'codex', 'frontend': 'antigravity', 'review': 'claude', 'decide': 'claude'}
# Gemini 4 Hydrogen (High) ranks highest for web UI among the models an Antigravity plan offers, at moderate quota.
DEFAULT_ANTIGRAVITY_MODEL = 'gemini-4-hydrogen-high'
DEFAULT_PLAN_MODEL = 'opus'
