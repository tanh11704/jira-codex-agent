from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

USER_CONFIG_PATH = Path("~/.config/jira-codex-agent/config.env").expanduser()


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

    @field_validator("codex_model", mode="before")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None
    quota_remaining_threshold: int = Field(default=30, ge=0, le=100)
    quota_weekly_remaining_threshold: int = Field(default=30, ge=0, le=100)
    max_task_seconds: int = Field(default=7200, ge=60)
    dry_run: bool = False

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
