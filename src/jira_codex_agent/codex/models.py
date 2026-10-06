from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class CodexRunResult(BaseModel):
    outcome: RunOutcome
    exit_code: int | None = None
    final_message: str = ""
    session_id: str | None = None
    events: int = 0
    error: str | None = None


class QuotaWindow(BaseModel):
    used_percent: int = Field(ge=0, le=100)
    window_minutes: int | None = None
    resets_at: datetime | None = None

    @property
    def remaining_percent(self) -> int:
        return 100 - self.used_percent


class QuotaSnapshot(BaseModel):
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    ordinary_usage_allowed: bool | None = None
    error: str | None = None

    def five_hour_window(self) -> QuotaWindow | None:
        windows = [window for window in (self.primary, self.secondary) if window]
        return min(windows, key=lambda item: abs((item.window_minutes or 0) - 300), default=None)
