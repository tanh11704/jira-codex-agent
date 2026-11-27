from __future__ import annotations

from pathlib import Path
import base64
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jira_codex_agent.agents import DEFAULT_ANTIGRAVITY_MODEL, DEFAULT_PLAN_MODEL

USER_CONFIG_PATH = Path("~/.config/jira-codex-agent/config.env").expanduser()
DEFAULT_TASK_INSTRUCTIONS = (Path(__file__).parent / "default_task_prompt.txt").read_text(encoding="utf-8").strip()
DEFAULT_DECIDE_INSTRUCTIONS = (Path(__file__).parent / "default_decide_prompt.txt").read_text(encoding="utf-8").strip()
UI_QUALITY_HEADING = "UI QUALITY AND VISUAL CHECK"
VISUAL_DESIGN_HEADING = "VISUAL DESIGN AND IMAGERY"


def _section(heading: str) -> str:
    """One rules paragraph of the default prompt: the heading up to the next blank line."""
    return heading + DEFAULT_TASK_INSTRUCTIONS.split(heading, 1)[1].split("\n\n", 1)[0]


UI_QUALITY_SECTION = _section(UI_QUALITY_HEADING)
VISUAL_DESIGN_SECTION = _section(VISUAL_DESIGN_HEADING)


def with_ui_quality(instructions: str) -> str:
    """Custom prompts saved before the UI rules existed must not silently drop them."""
    for heading, section in ((UI_QUALITY_HEADING, UI_QUALITY_SECTION), (VISUAL_DESIGN_HEADING, VISUAL_DESIGN_SECTION)):
        if heading not in instructions:
            instructions = f"{instructions}\n\n{section}"
    return instructions


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_prefix="JCA_",
        env_file=(".env", str(USER_CONFIG_PATH)),
        extra="ignore",
        case_sensitive=False,
    )

    jira_url: str
    jira_email: str
    jira_api_token: SecretStr
    jira_jql: str = "assignee = currentUser() AND statusCategory = 'To Do' ORDER BY priority DESC, created ASC"
    jira_poll_seconds: int = Field(default=600, ge=60)

    repository: Path
    base_branch: str = "main"
    worktree_root: Path = Path("~/.local/share/jira-codex-agent/worktrees")
    data_dir: Path = Path("~/.local/share/jira-codex-agent")
    socket_path: Path = Path("~/.local/share/jira-codex-agent/agent.sock")
    log_path: Path = Path("~/Library/Logs/jira-codex-agent/agent.log")

    codex_command: str = "codex"
    codex_model: str | None = None
    # Reasoning effort advertised by the model (e.g. low, medium, high, xhigh, max, ultra); None uses the model default.
    codex_reasoning_effort: str | None = None
    # Service tier id from the model catalog (e.g. "priority" for Fast); None uses standard speed.
    codex_service_tier: str | None = None
    codex_prompt_base64: str = ''
    # Sockets inside Codex's workspace-write sandbox (Docker, local servers, package downloads). Off by default: the
    # daemon runs Docker-backed checks and UI captures itself, so the agent needs neither nor spends tokens on them.
    codex_network_access: bool = False

    # Build/test commands the daemon runs itself in the worktree once an agent reports COMPLETED (base64, one per line).
    # Agents get only trimmed failure output back, instead of spending tokens on full build and test logs.
    verify_commands_base64: str = ''
    verify_fix_rounds: int = Field(default=2, ge=0, le=5)  # agent fix passes after a failed check; 0 only reports
    verify_timeout_seconds: int = Field(default=1800, ge=30)  # per command
    # A failing check is rerun on the base commit; failures already there are reported, not blamed on the task.
    baseline_checks: bool = True
    # A check that fails with a timeout is rerun this many times before it is blamed on the task (slow or flaky suites).
    verify_flaky_retries: int = Field(default=1, ge=0, le=3)
    # The daemon renders the screens an agent declares in "ui_captures" (built portal + API mocks) with headless Chrome,
    # fails on overflow/overlap/page errors and hands the screenshots to the reviewer; agents never take screenshots.
    ui_capture: bool = True
    baseline_setup_command: str = ''  # run once in the baseline worktree before its checks, e.g. pnpm install
    # Command approval requests matching these shell-style patterns are accepted without a human (base64, one per line).
    auto_approve_commands_base64: str = ''

    @field_validator('codex_prompt_base64', 'verify_commands_base64', 'auto_approve_commands_base64',
                     'claude_decide_prompt_base64')
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if value:
            base64.b64decode(value, validate=True).decode('utf-8')
        return value

    @property
    def task_instructions(self) -> str:
        text = base64.b64decode(self.codex_prompt_base64).decode('utf-8') if self.codex_prompt_base64 else ''
        return with_ui_quality(text.strip()) if text.strip() else DEFAULT_TASK_INSTRUCTIONS

    @property
    def decide_instructions(self) -> str:
        text = base64.b64decode(self.claude_decide_prompt_base64).decode('utf-8') if self.claude_decide_prompt_base64 else ''
        return text.strip() or DEFAULT_DECIDE_INSTRUCTIONS

    @property
    def verify_commands(self) -> list[str]:
        text = base64.b64decode(self.verify_commands_base64).decode('utf-8') if self.verify_commands_base64 else ''
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]

    @property
    def auto_approve_commands(self) -> list[str]:
        text = base64.b64decode(self.auto_approve_commands_base64).decode('utf-8') if self.auto_approve_commands_base64 else ''
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]
    codex_approvals_reviewer: Literal['user', 'auto_review'] = 'user'

    # Optional Claude Code passes around Codex, both read-only: a plan before coding and a review after COMPLETED.
    claude_command: str = "claude"
    claude_plan: bool = False
    claude_review: bool = False
    # Claude classifies each issue; frontend work is then coded by Claude Code (with write access) instead of Codex.
    # Full-stack issues split: Codex codes the backend (Claude reviews it), then Claude codes the frontend.
    claude_frontend: bool = False
    claude_model: str | None = None  # alias (opus, sonnet) or full model id; None uses the CLI default
    claude_effort: str | None = None  # low | medium | high | xhigh | max
    # Codex fix passes allowed after Claude requests changes; 0 only reports the review.
    claude_review_rounds: int = Field(default=1, ge=0, le=5)
    # Claude answers open product questions as the product owner (at planning and when the coder asks), so work
    # continues unattended; every decision it makes is summarized in the final report for the human to confirm.
    claude_decide: bool = False
    claude_decide_rounds: int = Field(default=2, ge=0, le=5)
    claude_decide_prompt_base64: str = ''  # the product owner's instructions; empty uses the default
    claude_timeout_seconds: int = Field(default=1800, ge=60)
    # Claude passes are skipped while the subscription's remaining 5-hour / weekly usage is at or below these.
    claude_quota_remaining_threshold: int = Field(default=30, ge=0, le=100)
    claude_quota_weekly_remaining_threshold: int = Field(default=30, ge=0, le=100)
    claude_max_turns: int = Field(default=0, ge=0)  # per pass; 0 means no limit

    # Antigravity CLI (agy): runs headless with --print; its models come from `agy models`.
    # The installer puts agy in ~/.local/bin, which launchd's PATH lacks.
    antigravity_command: str = str(_AGY) if (_AGY := Path('~/.local/bin/agy').expanduser()).exists() else "agy"
    antigravity_model: str | None = DEFAULT_ANTIGRAVITY_MODEL  # default for roles that use Antigravity

    # Who does each job. The claude_plan/review/frontend/decide switches above turn the passes on; these pick the agent.
    # An empty model/effort uses that agent's default (claude_model, antigravity_model, codex_model).
    plan_agent: Literal['claude', 'antigravity'] = 'claude'
    plan_model: str | None = DEFAULT_PLAN_MODEL
    plan_effort: str | None = None
    backend_agent: Literal['codex', 'claude', 'antigravity'] = 'codex'
    backend_model: str | None = None  # Codex always uses codex_model / codex_reasoning_effort
    backend_effort: str | None = None
    frontend_agent: Literal['antigravity', 'claude', 'codex'] = 'antigravity'
    frontend_model: str | None = None
    frontend_effort: str | None = None
    review_agent: Literal['claude', 'antigravity'] = 'claude'
    review_model: str | None = None
    review_effort: str | None = None
    decide_agent: Literal['claude', 'antigravity'] = 'claude'
    decide_model: str | None = None
    decide_effort: str | None = None

    @field_validator("codex_model", "codex_reasoning_effort", "codex_service_tier", "claude_model", "claude_effort",
                     "antigravity_model", "plan_model", "plan_effort", "backend_model", "backend_effort",
                     "frontend_model", "frontend_effort", "review_model", "review_effort", "decide_model",
                     "decide_effort", mode="before")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None
    quota_remaining_threshold: int = Field(default=30, ge=0, le=100)
    quota_weekly_remaining_threshold: int = Field(default=30, ge=0, le=100)
    max_task_seconds: int = Field(default=7200, ge=60)
    dry_run: bool = False

    # Unattended operation: nothing may wait on a human indefinitely.
    approval_timeout_seconds: int = Field(default=900, ge=0)  # 0 waits forever
    max_task_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: int = Field(default=600, ge=0)
    history_retention_days: int = Field(default=30, ge=1)
    max_events_per_task: int = Field(default=500, ge=50)
    prevent_sleep: bool = True
    notifications: bool = True

    @field_validator("jira_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("repository", "worktree_root", "data_dir", "socket_path", "log_path")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.db"
