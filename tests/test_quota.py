from datetime import datetime, timezone

from jira_codex_agent.codex.quota import QuotaReader


def test_parse_quota_snapshot() -> None:
    snapshot = QuotaReader._parse(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "primary": {"usedPercent": 72, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
                "secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": None},
            },
        }
    )
    assert snapshot.primary.remaining_percent == 28
    assert snapshot.primary.resets_at == datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
