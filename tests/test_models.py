from datetime import datetime, timezone

from jira_codex_agent.codex.models import QuotaSnapshot, QuotaWindow
from jira_codex_agent.jira.models import adf_to_text


def test_adf_to_text_flattens_paragraphs() -> None:
    document = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]}
    assert adf_to_text(document) == "Hello\n"


def test_five_hour_window_chooses_nearest_duration() -> None:
    snapshot = QuotaSnapshot(
        primary=QuotaWindow(used_percent=10, window_minutes=300, resets_at=datetime.now(timezone.utc)),
        secondary=QuotaWindow(used_percent=20, window_minutes=10080),
    )
    assert snapshot.five_hour_window() is snapshot.primary
    assert snapshot.five_hour_window().remaining_percent == 90
